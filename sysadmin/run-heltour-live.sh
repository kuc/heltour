#!/bin/bash
cd /var/www/www.lichess4545.com/
export PYTHONPATH=/var/www/www.lichess4545.com/
/var/www/www.lichess4545.com/env/bin/gunicorn --capture-output --error-logfile /var/log/heltour/error.log -t 300 -w 4 -b 127.0.0.1:8580  heltour.wsgi:application

