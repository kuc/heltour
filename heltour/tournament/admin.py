from django.contrib import admin, messages
from django.utils import timezone
from heltour.tournament import lichessapi, slackapi, views, forms
from heltour.tournament.models import *
from reversion.admin import VersionAdmin
from django.conf.urls import url
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import permission_required
import reversion

import json
import pairinggen
import spreadsheet
from django.db.models.query import Prefetch
from django.db import transaction
from smtplib import SMTPException
from django.template.loader import render_to_string
from django.core.mail import send_mail
from heltour import settings
from datetime import timedelta
from django_comments.models import Comment
from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from django.http.response import HttpResponse
from django.utils.http import urlquote
from django.core.mail.message import EmailMultiAlternatives
from django.core import mail
from django.utils.html import format_html

# Customize which sections are visible
# admin.site.register(Comment)
admin.site.unregister(Site)

def redirect_with_params(*args, **kwargs):
    params = kwargs.pop('params')
    response = redirect(*args, **kwargs)
    response['Location'] += params
    print 'Redirect: ', response['Location']
    return response

#-------------------------------------------------------------------------------
@admin.register(League)
class LeagueAdmin(VersionAdmin):
    actions = ['import_season']
    change_form_template = 'tournament/admin/change_form_with_comments.html'

    def get_urls(self):
        urls = super(LeagueAdmin, self).get_urls()
        my_urls = [
            url(r'^(?P<object_id>[0-9]+)/import_season/$',
                permission_required('tournament.change_league')(self.admin_site.admin_view(self.import_season_view)),
                name='import_season'),
        ]
        return my_urls + urls

    def import_season(self, request, queryset):
        return redirect('admin:import_season', object_id=queryset[0].pk)

    def import_season_view(self, request, object_id):
        league = get_object_or_404(League, pk=object_id)

        if request.method == 'POST':
            form = forms.ImportSeasonForm(request.POST)
            if form.is_valid():
                try:
                    if league.competitor_type == 'team':
                        spreadsheet.import_team_season(league, form.cleaned_data['spreadsheet_url'], form.cleaned_data['season_name'], form.cleaned_data['season_tag'],
                                                  form.cleaned_data['rosters_only'], form.cleaned_data['exclude_live_pairings'])
                        self.message_user(request, "Season imported.")
                    elif league.competitor_type == 'individual':
                        spreadsheet.import_lonewolf_season(league, form.cleaned_data['spreadsheet_url'], form.cleaned_data['season_name'], form.cleaned_data['season_tag'],
                                                           form.cleaned_data['rosters_only'], form.cleaned_data['exclude_live_pairings'])
                        self.message_user(request, "Season imported.")
                    else:
                        self.message_user(request, "League competitor type not supported for spreadsheet import")
                except spreadsheet.SpreadsheetNotFound:
                    self.message_user(request, "Spreadsheet not found. The service account may not have edit permissions.", messages.ERROR)
                return redirect('admin:tournament_league_changelist')
        else:
            form = forms.ImportSeasonForm()

        context = {
            'has_permission': True,
            'opts': self.model._meta,
            'site_url': '/',
            'original': league,
            'title': 'Import season',
            'form': form
        }

        return render(request, 'tournament/admin/import_season.html', context)

#-------------------------------------------------------------------------------
@admin.register(Season)
class SeasonAdmin(VersionAdmin):
    list_display = ('__unicode__', 'league',)
    list_display_links = ('__unicode__',)
    list_filter = ('league',)
    actions = ['update_board_order_by_rating', 'recalculate_scores', 'verify_data', 'review_nominated_games', 'bulk_email', 'manage_players', 'round_transition']
    change_form_template = 'tournament/admin/change_form_with_comments.html'

    def get_urls(self):
        urls = super(SeasonAdmin, self).get_urls()
        my_urls = [
            url(r'^(?P<object_id>[0-9]+)/manage_players/$',
                permission_required('tournament.manage_players')(self.admin_site.admin_view(self.manage_players_view)),
                name='manage_players'),
            url(r'^(?P<object_id>[0-9]+)/player_info/(?P<player_name>[\w-]+)/$',
                permission_required('tournament.manage_players')(self.admin_site.admin_view(self.player_info_view)),
                name='edit_rosters_player_info'),
            url(r'^(?P<object_id>[0-9]+)/round_transition/$',
                permission_required('tournament.generate_pairings')(self.admin_site.admin_view(self.round_transition_view)),
                name='round_transition'),
            url(r'^(?P<object_id>[0-9]+)/review_nominated_games/$',
                permission_required('tournament.review_nominated_games')(self.admin_site.admin_view(self.review_nominated_games_view)),
                name='review_nominated_games'),
            url(r'^(?P<object_id>[0-9]+)/review_nominated_games/select/(?P<nom_id>[0-9]+)/$',
                permission_required('tournament.review_nominated_games')(self.admin_site.admin_view(self.review_nominated_games_select_view)),
                name='review_nominated_games_select'),
            url(r'^(?P<object_id>[0-9]+)/review_nominated_games/deselect/(?P<sel_id>[0-9]+)/$',
                permission_required('tournament.review_nominated_games')(self.admin_site.admin_view(self.review_nominated_games_deselect_view)),
                name='review_nominated_games_deselect'),
            url(r'^(?P<object_id>[0-9]+)/review_nominated_games/pgn/$',
                permission_required('tournament.review_nominated_games')(self.admin_site.admin_view(self.review_nominated_games_pgn_view)),
                name='review_nominated_games_pgn'),
            url(r'^(?P<object_id>[0-9]+)/bulk_email/$',
                permission_required('tournament.bulk_email')(self.admin_site.admin_view(self.bulk_email_view)),
                name='bulk_email'),
        ]
        return my_urls + urls

    def recalculate_scores(self, request, queryset):
        for season in queryset:
            if season.league.competitor_type == 'team':
                for team_pairing in TeamPairing.objects.filter(round__season=season):
                    team_pairing.refresh_points()
                    team_pairing.save()
            season.calculate_scores()
        self.message_user(request, 'Scores recalculated.', messages.INFO)

    def verify_data(self, request, queryset):
        for season in queryset:
            # Ensure SeasonPlayer objects exist for all paired players
            if season.league.competitor_type == 'team':
                pairings = TeamPlayerPairing.objects.filter(team_pairing__round__season=season)
            else:
                pairings = LonePlayerPairing.objects.filter(round__season=season)
            for p in pairings:
                SeasonPlayer.objects.get_or_create(season=season, player=p.white)
                SeasonPlayer.objects.get_or_create(season=season, player=p.black)
            # Normalize all gamelinks
            bad_gamelinks = 0
            for p in pairings:
                old = p.game_link
                p.game_link, ok = normalize_gamelink(old)
                if not ok:
                    bad_gamelinks += 1
                if p.game_link != old:
                    p.save()
            if bad_gamelinks > 0:
                self.message_user(request, '%d bad gamelinks for %s.' % (bad_gamelinks, season.name), messages.WARNING)
        self.message_user(request, 'Data verified.', messages.INFO)

    def review_nominated_games(self, request, queryset):
        if queryset.count() > 1:
            self.message_user(request, 'Nominated games can only be reviewed one season at a time.', messages.ERROR)
            return
        return redirect('admin:review_nominated_games', object_id=queryset[0].pk)

    def review_nominated_games_view(self, request, object_id):
        season = get_object_or_404(Season, pk=object_id)

        selections = GameSelection.objects.filter(season=season).order_by('pairing__teamplayerpairing__board_number')
        nominations = GameNomination.objects.filter(season=season).order_by('pairing__teamplayerpairing__board_number', 'date_created')

        selected_links = set((s.game_link for s in selections))

        link_counts = {}
        link_to_nom = {}
        first_nominations = []
        for n in nominations:
            value = link_counts.get(n.game_link, 0)
            if value == 0:
                first_nominations.append(n)
                link_to_nom[n.game_link] = n
            link_counts[n.game_link] = value + 1

        selections = [(link_counts.get(s.game_link, 0), s, link_to_nom.get(s.game_link, None)) for s in selections]
        nominations = [(link_counts.get(n.game_link, 0), n) for n in first_nominations if n.game_link not in selected_links]

        if season.nominations_open:
            self.message_user(request, 'Nominations are still open. You should edit the season and close nominations before reviewing.', messages.WARNING)

        context = {
            'has_permission': True,
            'opts': self.model._meta,
            'site_url': '/',
            'original': season,
            'title': 'Review nominated games',
            'selections': selections,
            'nominations': nominations,
            'is_team': season.league.competitor_type == 'team',
        }

        return render(request, 'tournament/admin/review_nominated_games.html', context)

    def review_nominated_games_select_view(self, request, object_id, nom_id):
        season = get_object_or_404(Season, pk=object_id)
        nom = get_object_or_404(GameNomination, pk=nom_id)

        GameSelection.objects.get_or_create(season=season, game_link=nom.game_link, defaults={'pairing': nom.pairing})

        return redirect('admin:review_nominated_games', object_id=object_id)

    def review_nominated_games_deselect_view(self, request, object_id, sel_id):
        gs = GameSelection.objects.filter(pk=sel_id).first()
        if gs is not None:
            gs.delete()

        return redirect('admin:review_nominated_games', object_id=object_id)

    def review_nominated_games_pgn_view(self, request, object_id):
        gamelink = request.GET.get('gamelink')
        gameid = get_gameid_from_gamelink(gamelink)
        pgn = lichessapi.get_pgn_with_cache(gameid, priority=10)

        # Strip most tags for "blind" review
        pgn = re.sub('\[[^R]\w+ ".*"\]\n', '', pgn)

        return HttpResponse(pgn)

    def round_transition(self, request, queryset):
        if queryset.count() > 1:
            self.message_user(request, 'Rounds can only be transitioned one season at a time.', messages.ERROR)
            return
        return redirect('admin:round_transition', object_id=queryset[0].pk)

    def round_transition_view(self, request, object_id):
        season = get_object_or_404(Season, pk=object_id)

        round_to_close = season.round_set.filter(publish_pairings=True, is_completed=False).order_by('number').first()
        round_to_open = season.round_set.filter(publish_pairings=False, is_completed=False).order_by('number').first()

        season_to_close = season if not season.is_completed and round_to_open is None and (round_to_close is None or round_to_close.number == season.rounds) else None

        if request.method == 'POST':
            form = forms.RoundTransitionForm(season.league.competitor_type == 'team', round_to_close, round_to_open, season_to_close, request.POST)
            if form.is_valid():
                with transaction.atomic():
                    if 'round_to_close' in form.cleaned_data and form.cleaned_data['round_to_close'] == round_to_close.number:
                        if form.cleaned_data['complete_round']:
                            with reversion.create_revision():
                                reversion.set_user(request.user)
                                reversion.set_comment('Close round')
                                round_to_close.is_completed = True
                                round_to_close.save()
                            self.message_user(request, 'Round %d set as completed.' % round_to_close.number, messages.INFO)
                    if 'complete_season' in form.cleaned_data and season_to_close is not None and form.cleaned_data['complete_season'] \
                            and (round_to_close is None or round_to_close.is_completed):
                        with reversion.create_revision():
                            reversion.set_user(request.user)
                            reversion.set_comment('Close season')
                            season_to_close.is_completed = True
                            season_to_close.save()
                        self.message_user(request, '%s set as completed.' % season_to_close.name, messages.INFO)
                    if 'round_to_open' in form.cleaned_data and form.cleaned_data['round_to_open'] == round_to_open.number:
                        if 'update_board_order' in form.cleaned_data and form.cleaned_data['update_board_order']:
                            try:
                                with reversion.create_revision():
                                    reversion.set_user(request.user)
                                    reversion.set_comment('Update board order')
                                    self.do_update_board_order(season)
                                self.message_user(request, 'Board order updated.', messages.INFO)
                            except IndexError:
                                self.message_user(request, 'Error updating board order.', messages.ERROR)
                                return redirect('admin:round_transition', object_id)
                        if form.cleaned_data['generate_pairings']:
                            try:
                                with reversion.create_revision():
                                    reversion.set_user(request.user)
                                    reversion.set_comment('Generate pairings')
                                    pairinggen.generate_pairings(round_to_open, overwrite=False)
                                    round_to_open.publish_pairings = False
                                    round_to_open.save()
                                self.message_user(request, 'Pairings generated.', messages.INFO)
                                return redirect('admin:review_pairings', round_to_open.pk)
                            except pairinggen.PairingsExistException:
                                self.message_user(request, 'Unpublished pairings already exist.', messages.WARNING)
                                return redirect('admin:review_pairings', round_to_open.pk)
                            except pairinggen.PairingHasResultException:
                                self.message_user(request, 'Pairings with results can\'t be overwritten.', messages.ERROR)
                    return redirect('admin:tournament_season_changelist')
        else:
            form = forms.RoundTransitionForm(season.league.competitor_type == 'team', round_to_close, round_to_open, season_to_close)

        if round_to_close is not None and round_to_close.end_date is not None and round_to_close.end_date > timezone.now() + timedelta(hours=1):
            time_from_now = self._time_from_now(round_to_close.end_date - timezone.now())
            self.message_user(request, 'The round %d end date is %s from now.' % (round_to_close.number, time_from_now), messages.WARNING)
        elif round_to_open is not None and round_to_open.start_date is not None and round_to_open.start_date > timezone.now() + timedelta(hours=1):
            time_from_now = self._time_from_now(round_to_open.start_date - timezone.now())
            self.message_user(request, 'The round %d start date is %s from now.' % (round_to_open.number, time_from_now), messages.WARNING)

        if round_to_close is not None:
            incomplete_pairings = PlayerPairing.objects.filter(result='', teamplayerpairing__team_pairing__round=round_to_close).nocache() | \
                                  PlayerPairing.objects.filter(result='', loneplayerpairing__round=round_to_close).nocache()
            if len(incomplete_pairings) > 0:
                self.message_user(request, 'Round %d has %d pairing(s) without a result.' % (round_to_close.number, len(incomplete_pairings)), messages.WARNING)

        context = {
            'has_permission': True,
            'opts': self.model._meta,
            'site_url': '/',
            'original': season,
            'title': 'Round transition',
            'form': form
        }

        return render(request, 'tournament/admin/round_transition.html', context)

    def bulk_email(self, request, queryset):
        if queryset.count() > 1:
            self.message_user(request, 'Emails can only be sent one season at a time.', messages.ERROR)
            return
        return redirect('admin:bulk_email', object_id=queryset[0].pk)

    def bulk_email_view(self, request, object_id):
        season = get_object_or_404(Season, pk=object_id)

        if request.method == 'POST':
            form = forms.BulkEmailForm(season, request.POST)
            if form.is_valid() and form.cleaned_data['confirm_send']:
                season_players = season.seasonplayer_set.all()
                email_addresses = {sp.player.email for sp in season_players if sp.player.email != ''}
                email_messages = []
                for addr in email_addresses:
                    message = EmailMultiAlternatives(
                        form.cleaned_data['subject'],
                        form.cleaned_data['text_content'],
                        settings.DEFAULT_FROM_EMAIL,
                        [addr]
                    )
                    message.attach_alternative(form.cleaned_data['html_content'], 'text/html')
                    email_messages.append(message)
                conn = mail.get_connection()
                conn.open()
                conn.send_messages(email_messages)
                conn.close()
                self.message_user(request, 'Emails sent to %d players.' % len(season_players), messages.INFO)
                return redirect('admin:tournament_season_changelist')
        else:
            form = forms.BulkEmailForm(season)

        context = {
            'has_permission': True,
            'opts': self.model._meta,
            'site_url': '/',
            'original': season,
            'title': 'Bulk email',
            'form': form
        }

        return render(request, 'tournament/admin/bulk_email.html', context)

    def _time_from_now(self, delta):
        if delta.days > 0:
            if delta.days == 1:
                return '1 day'
            else:
                return '%d days' % delta.days
        else:
            hours = delta.seconds / 3600
            if hours == 1:
                return '1 hour'
            else:
                return '%d hours' % hours

    def update_board_order_by_rating(self, request, queryset):
        try:
            for season in queryset.all():
                with reversion.create_revision():
                    reversion.set_user(request.user)
                    reversion.set_comment('Update board order')

                    self.do_update_board_order(season)
            self.message_user(request, 'Board order updated.', messages.INFO)
        except IndexError:
            self.message_user(request, 'Error updating board order.', messages.ERROR)

    def do_update_board_order(self, season):
        if season.league.competitor_type != 'team':
            return

        # Update board order in teams
        for team in season.team_set.all():
            members = list(team.teammember_set.all())
            members.sort(key=lambda m: m.player.rating, reverse=True)
            occupied_boards = [m.board_number for m in members]
            occupied_boards.sort()
            for i, board_number in enumerate(occupied_boards):
                m = members[i]
                if m.board_number != board_number:
                    TeamMember.objects.update_or_create(team=team, board_number=board_number, \
                                                           defaults={ 'player': m.player, 'is_captain': m.is_captain,
                                                                      'is_vice_captain': m.is_vice_captain })

        # Update alternate buckets
        members_by_board = [TeamMember.objects.filter(team__season=season, board_number=n + 1) for n in range(season.boards)]
        ratings_by_board = [sorted([float(m.player.rating) for m in m_list]) for m_list in members_by_board]
        # Calculate the average of the upper/lower half of each board (minus the most extreme value to avoid outliers skewing the average)
        left_average_by_board = [sum(r_list[1:int(len(r_list) / 2)]) / (int(len(r_list) / 2) - 1) if len(r_list) > 2 else sum(r_list) / len(r_list) if len(r_list) > 0 else None for r_list in ratings_by_board]
        right_average_by_board = [sum(r_list[int((len(r_list) + 1) / 2):-1]) / (int(len(r_list) / 2) - 1) if len(r_list) > 2 else sum(r_list) / len(r_list) if len(r_list) > 0 else None for r_list in ratings_by_board]
        boundaries = []
        for i in range(season.boards + 1):
            # The logic here is a bit complicated in order to handle cases where there are no players for a board
            left_i = i - 1
            while left_i >= 0 and left_average_by_board[left_i] is None:
                left_i -= 1
            left = left_average_by_board[left_i] if left_i >= 0 else None
            right_i = i
            while right_i < season.boards and right_average_by_board[right_i] is None:
                right_i += 1
            right = right_average_by_board[right_i] if right_i < season.boards else None
            if left is None or right is None:
                boundaries.append(None)
            else:
                boundaries.append((left + right) / 2)
        for board_num in range(1, season.boards + 1):
            min_rating = boundaries[board_num]
            max_rating = boundaries[board_num - 1]
            if min_rating is None and max_rating is None:
                AlternateBucket.objects.filter(season=season, board_number=board_num).delete()
            else:
                AlternateBucket.objects.update_or_create(season=season, board_number=board_num, defaults={ 'max_rating': max_rating, 'min_rating': min_rating })

        # Assign alternates to buckets
        for alt in Alternate.objects.filter(season_player__season=season):
            alt.update_board_number()

    def manage_players(self, request, queryset):
        if queryset.count() > 1:
            self.message_user(request, 'Players can only be managed one season at a time.', messages.ERROR)
            return
        return redirect('admin:manage_players', object_id=queryset[0].pk)

    def player_info_view(self, request, object_id, player_name):
        season = get_object_or_404(Season, pk=object_id)
        season_player = get_object_or_404(SeasonPlayer, season=season, player__lichess_username=player_name)
        player = season_player.player

        reg = season_player.registration
        if player.games_played is not None:
            has_played_20_games = player.games_played >= 20
        else:
            has_played_20_games = reg is not None and reg.has_played_20_games

        context = {
            'season_player': season_player,
            'player': season_player.player,
            'reg': reg,
            'has_played_20_games': has_played_20_games
        }

        return render(request, 'tournament/admin/edit_rosters_player_info.html', context)

    def manage_players_view(self, request, object_id):
        season = get_object_or_404(Season, pk=object_id)
        if season.league.competitor_type == 'team':
            return self.team_manage_players_view(request, object_id)
        else:
            return self.lone_manage_players_view(request, object_id)

    def team_manage_players_view(self, request, object_id):
        season = get_object_or_404(Season, pk=object_id)
        teams_locked = bool(Round.objects.filter(season=season, publish_pairings=True).count())

        if request.method == 'POST':
            form = forms.EditRostersForm(request.POST)
            if form.is_valid():
                changes = json.loads(form.cleaned_data['changes'])
                # raise ValueError(changes)
                has_error = False
                for change in changes:
                    try:
                        if change['action'] == 'change-member':
                            with reversion.create_revision():
                                reversion.set_user(request.user)
                                reversion.set_comment('Edit rosters - change team member')

                                team_num = change['team_number']
                                team = Team.objects.get(season=season, number=team_num)

                                board_num = change['board_number']
                                player_info = change['player']

                                teammember = TeamMember.objects.filter(team=team, board_number=board_num).first()
                                if teammember == None:
                                    teammember = TeamMember(team=team, board_number=board_num)
                                if player_info is None:
                                    teammember.delete()
                                else:
                                    teammember.player = Player.objects.get(lichess_username=player_info['name'])
                                    teammember.is_captain = player_info['is_captain']
                                    teammember.is_vice_captain = player_info['is_vice_captain']
                                    teammember.save()

                        if change['action'] == 'change-team' and not teams_locked:
                            with reversion.create_revision():
                                reversion.set_user(request.user)
                                reversion.set_comment('Edit rosters - change team')

                                team_num = change['team_number']
                                team = Team.objects.get(season=season, number=team_num)

                                team_name = change['team_name']
                                team.name = team_name
                                team.save()

                        if change['action'] == 'create-team' and not teams_locked:
                            with reversion.create_revision():
                                reversion.set_user(request.user)
                                reversion.set_comment('Edit rosters - create team')

                                model = change['model']
                                team = Team.objects.create(season=season, number=model['number'], name=model['name'])

                                for board_num, player_info in enumerate(model['boards'], 1):
                                    if player_info is not None:
                                        player = Player.objects.get(lichess_username=player_info['name'])
                                        is_captain = player_info['is_captain']
                                        TeamMember.objects.create(team=team, player=player, board_number=board_num, is_captain=is_captain)

                        if change['action'] == 'create-alternate':
                            with reversion.create_revision():
                                reversion.set_user(request.user)
                                reversion.set_comment('Edit rosters - create alternate')

                                board_num = change['board_number']
                                season_player = SeasonPlayer.objects.get(season=season, player__lichess_username__iexact=change['player_name'])
                                Alternate.objects.update_or_create(season_player=season_player, defaults={ 'board_number': board_num })

                        if change['action'] == 'delete-alternate':
                            with reversion.create_revision():
                                reversion.set_user(request.user)
                                reversion.set_comment('Edit rosters - delete alternate')

                                board_num = change['board_number']
                                season_player = SeasonPlayer.objects.get(season=season, player__lichess_username__iexact=change['player_name'])
                                alt = Alternate.objects.filter(season_player=season_player, board_number=board_num).first()
                                if alt is not None:
                                    alt.delete()

                    except Exception:
                        has_error = True

                if has_error:
                    self.message_user(request, 'Some changes could not be saved.', messages.WARNING)

                if 'save_continue' in form.data:
                    return redirect('admin:manage_players', object_id)
                return redirect('admin:tournament_season_changelist')
        else:
            form = forms.EditRostersForm()

        board_numbers = list(range(1, season.boards + 1))
        teams = list(Team.objects.filter(season=season).order_by('number').prefetch_related(
            Prefetch('teammember_set', queryset=TeamMember.objects.select_related('player').nocache())
        ).nocache())
        team_members = TeamMember.objects.filter(team__season=season).select_related('player').nocache()
        alternates = Alternate.objects.filter(season_player__season=season).select_related('season_player__player').nocache()
        alternates_by_board = [(n, sorted(
                                          alternates.filter(board_number=n).select_related('season_player__registration').nocache(),
                                          key=lambda alt: alt.priority_date()
                                         )) for n in board_numbers]

        season_player_objs = SeasonPlayer.objects.filter(season=season, is_active=True).select_related('player', 'registration').nocache()
        season_players = set(sp.player for sp in season_player_objs)
        team_players = set(tm.player for tm in team_members)
        alternate_players = set(alt.season_player.player for alt in alternates)

        alternate_buckets = list(AlternateBucket.objects.filter(season=season))
        unassigned_players = list(sorted(season_players - team_players - alternate_players, key=lambda p: p.rating, reverse=True))
        if len(alternate_buckets) == season.boards:
            # Sort unassigned players by alternate buckets
            unassigned_by_board = [(n, [p for p in unassigned_players if find(alternate_buckets, board_number=n).contains(p.rating)]) for n in board_numbers]
        else:
            # Season doesn't have buckets yet. Sort by player soup
            sorted_players = list(sorted((p for p in season_players if p.rating is not None), key=lambda p: p.rating, reverse=True))
            player_count = len(sorted_players)
            unassigned_by_board = [(n, []) for n in board_numbers]
            if player_count > 0:
                max_ratings = [(n, sorted_players[len(sorted_players) * (n - 1) / season.boards].rating) for n in board_numbers]
                for p in unassigned_players:
                    board_num = 1
                    for n, max_rating in max_ratings:
                        if p.rating <= max_rating:
                            board_num = n
                        else:
                            break
                    unassigned_by_board[board_num - 1][1].append(p)

        if teams_locked:
            new_team_number = None
        elif len(teams) == 0:
            new_team_number = 1
        else:
            new_team_number = teams[-1].number + 1

        # Player highlights
        red_players = set()
        blue_players = set()
        for sp in season_player_objs:
            reg = sp.registration
            if sp.player.games_played is not None:
                if sp.player.games_played < 20:
                    red_players.add(sp.player)
            elif reg is None or not reg.has_played_20_games:
                red_players.add(sp.player)
            if not sp.player.in_slack_group:
                red_players.add(sp.player)
            if sp.games_missed >= 2:
                red_players.add(sp.player)
            if reg is not None and reg.alternate_preference == 'alternate':
                    blue_players.add(sp.player)

        expected_ratings = {sp.player: sp.expected_rating() for sp in season_player_objs}

        context = {
            'has_permission': True,
            'opts': self.model._meta,
            'site_url': '/',
            'original': season,
            'title': 'Edit rosters',
            'form': form,
            'teams': teams,
            'teams_locked': teams_locked,
            'new_team_number': new_team_number,
            'alternates_by_board': alternates_by_board,
            'unassigned_by_board': unassigned_by_board,
            'board_numbers': board_numbers,
            'board_count': season.boards,
            'red_players': red_players,
            'blue_players': blue_players,
            'expected_ratings': expected_ratings,
        }

        return render(request, 'tournament/admin/edit_rosters.html', context)

    def lone_manage_players_view(self, request, object_id):
        season = get_object_or_404(Season, pk=object_id)

        active_players = SeasonPlayer.objects.filter(season=season, is_active=True).order_by('player__lichess_username')
        inactive_players = SeasonPlayer.objects.filter(season=season, is_active=False).order_by('player__lichess_username')

        def get_data(r):
            regs = r.playerlateregistration_set.order_by('player__lichess_username')
            wds = r.playerwithdrawl_set.order_by('player__lichess_username')
            byes = r.playerbye_set.order_by('player__lichess_username')
            unavailables = r.playeravailability_set.filter(is_available=False).order_by('player__lichess_username')

            # Don't show "unavailable" for players that already have a bye
            players_with_byes = {b.player for b in byes}
            unavailables = [u for u in unavailables if u.player not in players_with_byes]

            return r, regs, wds, byes, unavailables

        rounds = Round.objects.filter(season=season, is_completed=False).order_by('number')
        round_data = [get_data(r) for r in rounds]

        context = {
            'has_permission': True,
            'opts': self.model._meta,
            'site_url': '/',
            'original': season,
            'title': '',
            'active_players': active_players,
            'inactive_players': inactive_players,
            'round_data': round_data,
        }

        return render(request, 'tournament/admin/manage_lone_players.html', context)

@admin.register(Round)
class RoundAdmin(VersionAdmin):
    list_filter = ('season',)
    actions = ['generate_pairings']
    change_form_template = 'tournament/admin/change_form_with_comments.html'

    def get_urls(self):
        urls = super(RoundAdmin, self).get_urls()
        my_urls = [
            url(r'^(?P<object_id>[0-9]+)/generate_pairings/$',
                permission_required('tournament.generate_pairings')(self.admin_site.admin_view(self.generate_pairings_view)),
                name='generate_pairings'),
            url(r'^(?P<object_id>[0-9]+)/review_pairings/$',
                permission_required('tournament.generate_pairings')(self.admin_site.admin_view(self.review_pairings_view)),
                name='review_pairings'),
        ]
        return my_urls + urls

    def generate_pairings(self, request, queryset):
        if queryset.count() > 1:
            self.message_user(request, 'Pairings can only be generated one round at a time', messages.ERROR)
            return
        return redirect('admin:generate_pairings', object_id=queryset[0].pk)

    def generate_pairings_view(self, request, object_id):
        round_ = get_object_or_404(Round, pk=object_id)

        if request.method == 'POST':
            form = forms.GeneratePairingsForm(request.POST)
            if form.is_valid():
                try:
                    with reversion.create_revision():
                        reversion.set_user(request.user)
                        reversion.set_comment('Generate pairings')

                        pairinggen.generate_pairings(round_, overwrite=form.cleaned_data['overwrite_existing'])
                        round_.publish_pairings = False
                        round_.save()

                    self.message_user(request, 'Pairings generated.', messages.INFO)
                    return redirect('admin:review_pairings', object_id)
                except pairinggen.PairingsExistException:
                    if not round_.publish_pairings:
                        self.message_user(request, 'Unpublished pairings already exist.', messages.WARNING)
                        return redirect('admin:review_pairings', object_id)
                    self.message_user(request, 'Pairings already exist for the selected round.', messages.ERROR)
                except pairinggen.PairingHasResultException:
                    self.message_user(request, 'Pairings with results can\'t be overwritten.', messages.ERROR)
                return redirect('admin:generate_pairings', object_id=round_.pk)
        else:
            form = forms.GeneratePairingsForm()

        context = {
            'has_permission': True,
            'opts': self.model._meta,
            'site_url': '/',
            'original': round_,
            'title': 'Generate pairings',
            'form': form
        }

        return render(request, 'tournament/admin/generate_pairings.html', context)

    def review_pairings_view(self, request, object_id):
        round_ = get_object_or_404(Round, pk=object_id)

        if request.method == 'POST':
            form = forms.ReviewPairingsForm(request.POST)
            if form.is_valid():
                if 'publish' in form.data:
                    with reversion.create_revision():
                        reversion.set_user(request.user)
                        reversion.set_comment('Publish pairings')

                        round_.publish_pairings = True
                        round_.save()
                        # Update ranks in case of manual edits
                        rank_dict = lone_player_pairing_rank_dict(round_.season)
                        for lpp in round_.loneplayerpairing_set.all().nocache():
                            lpp.refresh_ranks(rank_dict)
                            lpp.save()
                        for bye in round_.playerbye_set.all():
                            bye.refresh_rank(rank_dict)
                            bye.save()
                    self.message_user(request, 'Pairings published.', messages.INFO)
                elif 'delete' in form.data:
                    try:
                        with reversion.create_revision():
                            reversion.set_user(request.user)
                            reversion.set_comment('Delete pairings')

                            pairinggen.delete_pairings(round_)

                        self.message_user(request, 'Pairings deleted.', messages.INFO)
                    except pairinggen.PairingHasResultException:
                        self.message_user(request, 'Pairings with results can\'t be deleted.', messages.ERROR)
                return redirect('admin:tournament_round_changelist')
        else:
            form = forms.ReviewPairingsForm()

        if round_.season.league.competitor_type == 'team':
            team_pairings = round_.teampairing_set.order_by('pairing_order')
            pairing_lists = [team_pairing.teamplayerpairing_set.order_by('board_number').nocache() for team_pairing in team_pairings]
            context = {
                'has_permission': True,
                'opts': self.model._meta,
                'site_url': '/',
                'original': round_,
                'title': 'Review pairings',
                'form': form,
                'pairing_lists': pairing_lists
            }
            return render(request, 'tournament/admin/review_team_pairings.html', context)
        else:
            pairings = round_.loneplayerpairing_set.order_by('pairing_order').nocache()
            byes = round_.playerbye_set.order_by('type', 'player_rank', 'player__lichess_username')
            next_pairing_order = 0
            for p in pairings:
                next_pairing_order = max(next_pairing_order, p.pairing_order + 1)

            # Find duplicate players
            player_refcounts = {}
            for p in pairings:
                player_refcounts[p.white] = player_refcounts.get(p.white, 0) + 1
                player_refcounts[p.black] = player_refcounts.get(p.black, 0) + 1
            for b in byes:
                player_refcounts[b.player] = player_refcounts.get(b.player, 0) + 1
            duplicate_players = {k for k, v in player_refcounts.items() if v > 1}

            active_players = {sp.player for sp in SeasonPlayer.objects.filter(season=round_.season, is_active=True)}

            def pairing_error(pairing):
                if not request.user.is_staff:
                    return None
                if pairing.white == None or pairing.black == None:
                    return 'Missing player'
                if pairing.white in duplicate_players:
                    return 'Duplicate player: %s' % pairing.white.lichess_username
                if pairing.black in duplicate_players:
                    return 'Duplicate player: %s' % pairing.black.lichess_username
                if not round_.is_completed and pairing.white not in active_players:
                    return 'Inactive player: %s' % pairing.white.lichess_username
                if not round_.is_completed and pairing.black not in active_players:
                    return 'Inactive player: %s' % pairing.black.lichess_username
                return None

            def bye_error(bye):
                if not request.user.is_staff:
                    return None
                if bye.player in duplicate_players:
                    return 'Duplicate player: %s' % bye.player.lichess_username
                if not round_.is_completed and bye.player not in active_players:
                    return 'Inactive player: %s' % bye.player.lichess_username
                return None

            # Add errors
            pairings = [(p, pairing_error(p)) for p in pairings]
            byes = [(b, bye_error(b)) for b in byes]

            context = {
                'has_permission': True,
                'opts': self.model._meta,
                'site_url': '/',
                'original': round_,
                'title': 'Review pairings',
                'form': form,
                'pairings': pairings,
                'byes': byes,
                'round_': round_,
                'next_pairing_order': next_pairing_order,
            }
            return render(request, 'tournament/admin/review_lone_pairings.html', context)


#-------------------------------------------------------------------------------
@admin.register(PlayerLateRegistration)
class PlayerLateRegistrationAdmin(VersionAdmin):
    list_display = ('__unicode__', 'retroactive_byes', 'late_join_points')
    search_fields = ('player__lichess_username',)
    list_filter = ('round__season', 'round__number')
    raw_id_fields = ('round', 'player')
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(PlayerWithdrawl)
class PlayerWithdrawlAdmin(VersionAdmin):
    list_display = ('__unicode__',)
    search_fields = ('player__lichess_username',)
    list_filter = ('round__season', 'round__number')
    raw_id_fields = ('round', 'player')
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(PlayerBye)
class PlayerByeAdmin(VersionAdmin):
    list_display = ('__unicode__', 'type')
    search_fields = ('player__lichess_username',)
    list_filter = ('round__season', 'round__number', 'type')
    raw_id_fields = ('round', 'player')
    exclude = ('player_rating',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(Player)
class PlayerAdmin(VersionAdmin):
    search_fields = ('lichess_username', 'email')
    list_filter = ('is_active',)
    actions = ['update_selected_player_ratings']
    change_form_template = 'tournament/admin/change_form_with_comments.html'

    def update_selected_player_ratings(self, request, queryset):
#         try:
        for player in queryset.all():
            rating, games_played = lichessapi.get_user_classical_rating_and_games_played(player.lichess_username, priority=1)
            player.rating = rating
            player.games_played = games_played
            player.save()
        self.message_user(request, 'Rating(s) updated', messages.INFO)
#         except:
#             self.message_user(request, 'Error updating rating(s) from lichess API', messages.ERROR)

#-------------------------------------------------------------------------------
@admin.register(LeagueModerator)
class LeagueModeratorAdmin(VersionAdmin):
    list_display = ('__unicode__', 'send_contact_emails')
    search_fields = ('player__lichess_username',)
    list_filter = ('league',)
    raw_id_fields = ('player',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
class TeamMemberInline(admin.TabularInline):
    model = TeamMember
    extra = 0
    ordering = ('board_number',)
    raw_id_fields = ('player',)
    exclude = ('player_rating',)

#-------------------------------------------------------------------------------
@admin.register(Team)
class TeamAdmin(VersionAdmin):
    list_display = ('name', 'season')
    search_fields = ('name',)
    list_filter = ('season',)
    inlines = [TeamMemberInline]
    actions = ['update_board_order_by_rating']
    change_form_template = 'tournament/admin/change_form_with_comments.html'

    def update_board_order_by_rating(self, request, queryset):
        for team in queryset.all():
            members = team.teammember_set.order_by('-player__rating')
            for i in range(len(members)):
                members[i].board_number = i + 1
                members[i].save()
        self.message_user(request, 'Board order updated', messages.INFO)

#-------------------------------------------------------------------------------
@admin.register(TeamMember)
class TeamMemberAdmin(VersionAdmin):
    list_display = ('__unicode__', 'team')
    search_fields = ('team__name', 'player__lichess_username')
    list_filter = ('team__season',)
    raw_id_fields = ('player',)
    exclude = ('player_rating',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(TeamScore)
class TeamScoreAdmin(VersionAdmin):
    list_display = ('team', 'match_points', 'game_points')
    search_fields = ('team__name',)
    list_filter = ('team__season',)
    raw_id_fields = ('team',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(Alternate)
class AlternateAdmin(VersionAdmin):
    list_display = ('__unicode__', 'board_number')
    search_fields = ('season_player__player__lichess_username',)
    list_filter = ('season_player__season', 'board_number')
    raw_id_fields = ('season_player',)
    exclude = ('player_rating',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(AlternateAssignment)
class AlternateAssignmentAdmin(VersionAdmin):
    list_display = ('__unicode__', 'player')
    search_fields = ('team__name', 'player__lichess_username')
    list_filter = ('round__season', 'round__number', 'board_number')
    raw_id_fields = ('round', 'team', 'player', 'replaced_player')
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(AlternateBucket)
class AlternateBucketAdmin(VersionAdmin):
    list_display = ('__unicode__', 'season')
    search_fields = ()
    list_filter = ('season', 'board_number')
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(TeamPairing)
class TeamPairingAdmin(VersionAdmin):
    list_display = ('white_team_name', 'black_team_name', 'season_name', 'round_number')
    search_fields = ('white_team__name', 'black_team__name')
    list_filter = ('round__season', 'round__number')
    raw_id_fields = ('white_team', 'black_team', 'round')
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(PlayerPairing)
class PlayerPairingAdmin(VersionAdmin):
    list_display = ('__unicode__', 'scheduled_time', 'game_link_url')
    search_fields = ('white__lichess_username', 'black__lichess_username', 'game_link')
    raw_id_fields = ('white', 'black')
    exclude = ('white_rating', 'black_rating', 'tv_state')
    change_form_template = 'tournament/admin/change_form_with_comments.html'

    def game_link_url(self, obj):
        if not obj.game_link:
            return ''
        return format_html("<a href='{url}'>{url}</a>", url=obj.game_link)

#-------------------------------------------------------------------------------
@admin.register(TeamPlayerPairing)
class TeamPlayerPairingAdmin(VersionAdmin):
    list_display = ('__unicode__', 'team_pairing', 'board_number', 'game_link_url')
    search_fields = ('white__lichess_username', 'black__lichess_username',
                     'team_pairing__white_team__name', 'team_pairing__black_team__name', 'game_link')
    list_filter = ('team_pairing__round__season', 'team_pairing__round__number',)
    raw_id_fields = ('white', 'black', 'team_pairing')
    change_form_template = 'tournament/admin/change_form_with_comments.html'

    def game_link_url(self, obj):
        if not obj.game_link:
            return ''
        return format_html("<a href='{url}'>{url}</a>", url=obj.game_link)

#-------------------------------------------------------------------------------
@admin.register(LonePlayerPairing)
class LonePlayerPairingAdmin(VersionAdmin):
    list_display = ('__unicode__', 'round', 'game_link_url')
    search_fields = ('white__lichess_username', 'black__lichess_username', 'game_link')
    list_filter = ('round__season', 'round__number')
    raw_id_fields = ('white', 'black', 'round')
    change_form_template = 'tournament/admin/change_form_with_comments.html'

    def game_link_url(self, obj):
        if not obj.game_link:
            return ''
        return format_html("<a href='{url}'>{url}</a>", url=obj.game_link)

#-------------------------------------------------------------------------------
@admin.register(Registration)
class RegistrationAdmin(VersionAdmin):
    list_display = ('review', 'email', 'status', 'season', 'date_created')
    list_display_links = ()
    search_fields = ('lichess_username', 'email', 'season__name')
    list_filter = ('status', 'season',)

    def changelist_view(self, request, extra_context=None):
        self.request = request
        return super(RegistrationAdmin, self).changelist_view(request, extra_context=extra_context)

    def review(self, obj):
        _url = reverse('admin:review_registration', args=[obj.pk]) + "?" + self.get_preserved_filters(self.request)
        return '<a href="%s"><b>%s</b></a>' % (_url, obj.lichess_username)
    review.allow_tags = True

    def edit(self, obj):
        return 'Edit'
    edit.allow_tags = True

    def get_urls(self):
        urls = super(RegistrationAdmin, self).get_urls()
        my_urls = [
            url(r'^(?P<object_id>[0-9]+)/review/$',
                permission_required('tournament.change_registration')(self.admin_site.admin_view(self.review_registration)),
                name='review_registration'),
            url(r'^(?P<object_id>[0-9]+)/approve/$',
                permission_required('tournament.change_registration')(self.admin_site.admin_view(self.approve_registration)),
                name='approve_registration'),
            url(r'^(?P<object_id>[0-9]+)/reject/$',
                permission_required('tournament.change_registration')(self.admin_site.admin_view(self.reject_registration)),
                name='reject_registration')
        ]
        return my_urls + urls

    def review_registration(self, request, object_id):
        reg = get_object_or_404(Registration, pk=object_id)

        if request.method == 'POST':
            changelist_filters = request.POST.get('_changelist_filters', '')
            form = forms.ReviewRegistrationForm(request.POST)
            if form.is_valid():
                params = '?_changelist_filters=' + urlquote(changelist_filters)
                if 'approve' in form.data and reg.status == 'pending':
                    return redirect_with_params('admin:approve_registration', object_id=object_id, params=params)
                elif 'reject' in form.data and reg.status == 'pending':
                    return redirect_with_params('admin:reject_registration', object_id=object_id, params=params)
                elif 'edit' in form.data:
                    return redirect_with_params('admin:tournament_registration_change', object_id, params=params)
                else:
                    return redirect_with_params('admin:tournament_registration_changelist', params=params)
        else:
            changelist_filters = request.GET.get('_changelist_filters', '')
            form = forms.ReviewRegistrationForm()

        is_team = reg.season.league.competitor_type == 'team'

        context = {
            'has_permission': True,
            'opts': self.model._meta,
            'site_url': '/',
            'original': reg,
            'title': 'Review registration',
            'form': form,
            'is_team': is_team,
            'changelist_filters': changelist_filters
        }

        return render(request, 'tournament/admin/review_registration.html', context)

    def approve_registration(self, request, object_id):
        reg = get_object_or_404(Registration, pk=object_id)

        if reg.status != 'pending':
            return redirect('admin:review_registration', object_id)

        if request.method == 'POST':
            changelist_filters = request.POST.get('_changelist_filters', '')
            form = forms.ApproveRegistrationForm(request.POST, registration=reg)
            if form.is_valid():
                if 'confirm' in form.data:
                    with reversion.create_revision():
                        reversion.set_user(request.user)
                        reversion.set_comment('Approve registration')

                        # Limit changes to moderators
                        mod = LeagueModerator.objects.filter(player__lichess_username__iexact=reg.lichess_username).first()
                        if mod is not None and mod.player.email and mod.player.email != reg.email:
                            reg.email = mod.player.email

                        # Add or update the player in the DB
                        player, created = Player.objects.update_or_create(
                            lichess_username__iexact=reg.lichess_username,
                            defaults={'lichess_username': reg.lichess_username, 'email': reg.email, 'is_active': True}
                        )
                        if player.rating is None:
                            # This is automatically set, so don't change it if we already have a rating
                            player.rating = reg.classical_rating
                            player.save()
                        if created and reg.already_in_slack_group:
                            # This is automatically set, so don't change it if the player already exists
                            player.in_slack_group = True
                            player.save()

                        SeasonPlayer.objects.update_or_create(
                            player=player,
                            season=reg.season,
                            defaults={'registration': reg, 'is_active': True}
                        )

                        if reg.season.league.competitor_type == 'team':
                            # Set availability
                            for week_number in reg.weeks_unavailable.split(','):
                                if week_number != '':
                                    round_ = Round.objects.filter(season=reg.season, number=int(week_number)).first()
                                    if round_ is not None:
                                        PlayerAvailability.objects.update_or_create(player=player, round=round_, defaults={'is_available': False})

                            subject = render_to_string('tournament/emails/team_registration_approved_subject.txt', {'reg': reg})
                            msg_plain = render_to_string('tournament/emails/team_registration_approved.txt', {'reg': reg})
                            msg_html = render_to_string('tournament/emails/team_registration_approved.html', {'reg': reg})
                        else:
                            # Create byes
                            for week_number in reg.weeks_unavailable.split(','):
                                if week_number != '':
                                    round_ = Round.objects.filter(season=reg.season, number=int(week_number)).first()
                                    if round_ is not None and not round_.publish_pairings:
                                        PlayerBye.objects.update_or_create(player=player, round=round_, defaults={'type': 'half-point-bye'})

                            if Round.objects.filter(season=reg.season, publish_pairings=True).count() > 0:
                                # Late registration
                                next_round = Round.objects.filter(season=reg.season, publish_pairings=False).order_by('number').first()
                                if next_round is not None:
                                    PlayerLateRegistration.objects.update_or_create(round=next_round, player=player,
                                                                          defaults={'retroactive_byes': form.cleaned_data['retroactive_byes'],
                                                                          'late_join_points': form.cleaned_data['late_join_points']})

                            subject = render_to_string('tournament/emails/lone_registration_approved_subject.txt', {'reg': reg})
                            msg_plain = render_to_string('tournament/emails/lone_registration_approved.txt', {'reg': reg})
                            msg_html = render_to_string('tournament/emails/lone_registration_approved.html', {'reg': reg})

                        if form.cleaned_data['send_confirm_email']:
                            try:
                                send_mail(
                                    subject,
                                    msg_plain,
                                    settings.DEFAULT_FROM_EMAIL,
                                    [reg.email],
                                    html_message=msg_html,
                                )
                                self.message_user(request, 'Confirmation email sent to "%s".' % reg.email, messages.INFO)
                            except SMTPException:
                                self.message_user(request, 'A confirmation email could not be sent.', messages.ERROR)

                        if form.cleaned_data['invite_to_slack']:
                            try:
                                slackapi.invite_user(reg.email)
                                self.message_user(request, 'Slack invitation sent to "%s".' % reg.email, messages.INFO)
                            except slackapi.AlreadyInTeam:
                                self.message_user(request, 'The player is already in the slack group.', messages.WARNING)
                            except slackapi.AlreadyInvited:
                                self.message_user(request, 'The player has already been invited to the slack group.', messages.WARNING)

                        reg.status = 'approved'
                        reg.status_changed_by = request.user.username
                        reg.status_changed_date = timezone.now()
                        reg.save()

                    self.message_user(request, 'Registration for "%s" approved.' % reg.lichess_username, messages.INFO)
                    return redirect_with_params('admin:tournament_registration_changelist', params='?' + changelist_filters)
                else:
                    return redirect_with_params('admin:review_registration', object_id, params='?_changelist_filters=' + urlquote(changelist_filters))
        else:
            changelist_filters = request.GET.get('_changelist_filters', '')
            form = forms.ApproveRegistrationForm(registration=reg)

        next_round = Round.objects.filter(season=reg.season, publish_pairings=False).order_by('number').first()

        mod = LeagueModerator.objects.filter(player__lichess_username__iexact=reg.lichess_username).first()
        no_email_change = mod is not None and mod.player.email and mod.player.email != reg.email
        confirm_email = mod.player.email if no_email_change else reg.email

        context = {
            'has_permission': True,
            'opts': self.model._meta,
            'site_url': '/',
            'original': reg,
            'title': 'Confirm approval',
            'form': form,
            'next_round': next_round,
            'confirm_email': confirm_email,
            'no_email_change': no_email_change,
            'changelist_filters': changelist_filters
        }

        return render(request, 'tournament/admin/approve_registration.html', context)

    def reject_registration(self, request, object_id):
        reg = get_object_or_404(Registration, pk=object_id)

        if reg.status != 'pending':
            return redirect('admin:review_registration', object_id)

        if request.method == 'POST':
            changelist_filters = request.POST.get('_changelist_filters', '')
            form = forms.RejectRegistrationForm(request.POST, registration=reg)
            if form.is_valid():
                if 'confirm' in form.data:
                    with reversion.create_revision():
                        reversion.set_user(request.user)
                        reversion.set_comment('Reject registration')

                        reg.status = 'rejected'
                        reg.status_changed_by = request.user.username
                        reg.status_changed_date = timezone.now()
                        reg.save()

                    self.message_user(request, 'Registration for "%s" rejected.' % reg.lichess_username, messages.INFO)
                    return redirect_with_params('admin:tournament_registration_changelist', params='?' + changelist_filters)
                else:
                    return redirect('admin:review_registration', object_id)
                    return redirect_with_params('admin:review_registration', object_id, params='?_changelist_filters=' + urlquote(changelist_filters))
        else:
            changelist_filters = request.GET.get('_changelist_filters', '')
            form = forms.RejectRegistrationForm(registration=reg)

        context = {
            'has_permission': True,
            'opts': self.model._meta,
            'site_url': '/',
            'original': reg,
            'title': 'Confirm rejection',
            'form': form,
            'changelist_filters': changelist_filters
        }

        return render(request, 'tournament/admin/reject_registration.html', context)

#-------------------------------------------------------------------------------
@admin.register(SeasonPlayer)
class SeasonPlayerAdmin(VersionAdmin):
    list_display = ('player', 'season', 'is_active', 'in_slack')
    search_fields = ('season__name', 'player__lichess_username')
    list_filter = ('season', 'is_active', 'player__in_slack_group')
    raw_id_fields = ('player', 'registration')
    change_form_template = 'tournament/admin/change_form_with_comments.html'

    def in_slack(self, sp):
        return sp.player.in_slack_group
    in_slack.boolean = True

#-------------------------------------------------------------------------------
@admin.register(LonePlayerScore)
class LonePlayerScoreAdmin(VersionAdmin):
    list_display = ('season_player', 'points', 'late_join_points')
    search_fields = ('season_player__season__name', 'season_player__player__lichess_username')
    list_filter = ('season_player__season',)
    raw_id_fields = ('season_player',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(PlayerAvailability)
class PlayerAvailabilityAdmin(VersionAdmin):
    list_display = ('player', 'round', 'is_available')
    search_fields = ('player__lichess_username',)
    list_filter = ('round__season', 'round__number')
    raw_id_fields = ('player', 'round')
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(SeasonPrize)
class SeasonPrizeAdmin(VersionAdmin):
    list_display = ('season', 'rank', 'max_rating')
    search_fields = ('season__name',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(SeasonPrizeWinner)
class SeasonPrizeWinnerAdmin(VersionAdmin):
    list_display = ('season_prize', 'player',)
    search_fields = ('season_prize__name', 'player__lichess_username')
    raw_id_fields = ('season_prize', 'player')
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(GameNomination)
class GameNominationAdmin(VersionAdmin):
    list_display = ('__unicode__',)
    search_fields = ('season__name', 'nominating_player__name')
    raw_id_fields = ('nominating_player',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(GameSelection)
class GameSelectionAdmin(VersionAdmin):
    list_display = ('__unicode__',)
    search_fields = ('season__name',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(AvailableTime)
class AvailableTimeAdmin(VersionAdmin):
    list_display = ('player', 'time', 'league')
    search_fields = ('player__lichess_username',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(NavItem)
class NavItemAdmin(VersionAdmin):
    list_display = ('__unicode__', 'parent')
    search_fields = ('text',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(ApiKey)
class ApiKeyAdmin(VersionAdmin):
    list_display = ('name',)
    search_fields = ('name',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(PrivateUrlAuth)
class PrivateUrlAuthAdmin(VersionAdmin):
    list_display = ('__unicode__', 'expires')
    search_fields = ('authenticated_user',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(Document)
class DocumentAdmin(VersionAdmin):
    list_display = ('name',)
    search_fields = ('name',)
    change_form_template = 'tournament/admin/change_form_with_comments.html'

#-------------------------------------------------------------------------------
@admin.register(LeagueDocument)
class LeagueDocumentAdmin(VersionAdmin):
    list_display = ('document', 'league', 'tag', 'type', 'url')
    search_fields = ('league__name', 'tag', 'document__name')
    change_form_template = 'tournament/admin/change_form_with_comments.html'

    def url(self, obj):
        _url = reverse('by_league:document', args=[obj.league.tag, obj.tag])
        return '<a href="%s">%s</a>' % (_url, _url)
    url.allow_tags = True

#-------------------------------------------------------------------------------
@admin.register(SeasonDocument)
class SeasonDocumentAdmin(VersionAdmin):
    list_display = ('document', 'season', 'tag', 'type', 'url')
    search_fields = ('season__name', 'tag', 'document__name')
    change_form_template = 'tournament/admin/change_form_with_comments.html'

    def url(self, obj):
        _url = reverse('by_league:by_season:document', args=[obj.season.league.tag, obj.season.tag, obj.tag])
        return '<a href="%s">%s</a>' % (_url, _url)
    url.allow_tags = True
