#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/chihai/workspace/biexce-social-backend-slim"

if [[ ! -f /etc/biexce-social.env ]]; then
  echo "Missing /etc/biexce-social.env; create it from ${ROOT}/.env.example first." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y nginx postgresql-client sqlite3
sudo install -m 0644 "${ROOT}/deploy/systemd/biexce-social.service" /etc/systemd/system/biexce-social.service
sudo install -m 0644 "${ROOT}/deploy/nginx/biexce-social.conf" /etc/nginx/sites-available/biexce-social
sudo ln -sfn /etc/nginx/sites-available/biexce-social /etc/nginx/sites-enabled/biexce-social
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable --now biexce-social nginx
sudo systemctl --no-pager --full status biexce-social
