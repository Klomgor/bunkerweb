#!/bin/sh

. /opt/scripts/utils.sh

# ask new certificates if needed
certbot renew --deploy-hook /opt/scripts/certbot-renew-hook.sh

if [ "$?" -eq 0 ] ; then
	job_log "[CERTBOT] renew operation done"
else
	job_log "[CERTBOT] renew operation failed"
fi

# fix rights
chown -R root:nginx /etc/letsencrypt
chmod -R 740 /etc/letsencrypt
find /etc/letsencrypt -type d -exec chmod 750 {} \;

# reload nginx
if [ -f /tmp/nginx.pid ] ; then
	/usr/sbin/nginx -s reload > /dev/null 2>&1
fi
