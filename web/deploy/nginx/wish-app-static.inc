# PROPOSAL ONLY. Include inside the EXISTING HTTPS server for app.wish-bot.ru.
# Replace that server's previous root/index/location / and matching headers;
# do not append duplicate locations or replace the global nginx configuration.
# Keep its existing listen/TLS/certificate settings and security headers.
# Do NOT include in port 80: leave the working HTTP ACME challenge unchanged.
# If HTTPS also has an ACME location, preserve its existing root/alias and add
# `auth_basic off;` there explicitly. Audit other child locations for auth bypass.
root /var/www/wish-app/current;
index index.html;
autoindex off;
satisfy all;
auth_basic "Wish preview - owner access";
auth_basic_user_file /etc/nginx/auth/wish-app.htpasswd;
add_header X-Robots-Tag "noindex, nofollow" always;
add_header Cache-Control "private, no-store" always;
add_header X-Content-Type-Options "nosniff" always;
error_page 404 /404.html;
location / {
    try_files $uri $uri/ =404;
}
