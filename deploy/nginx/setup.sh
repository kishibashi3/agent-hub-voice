#!/usr/bin/env bash
# setup.sh — Pi5 nginx HTTPS プロキシ ワンショットセットアップ
#
# 実行すること:
#   1. nginx がインストール済みか確認 (未インストールならガイドを表示)
#   2. 自己署名証明書を生成 (gen-certs.sh を呼び出す)
#   3. nginx 設定ファイルを sites-available に配置
#   4. sites-enabled にシンボリックリンクを作成
#   5. default 設定が 443 と競合する場合は無効化
#   6. nginx の設定を検証して reload
#
# Usage:
#   bash deploy/nginx/setup.sh [hostname]
#
# 引数:
#   hostname  Pi5 の mDNS ホスト名 (デフォルト: $(hostname).local)
#             例: raspberrypi.local / pi5.local
#
# 実行後にアクセスできる URL:
#   Voice (iPhone):  https://<Pi5 IP> または https://<hostname>
#   Dashboard:       https://<Pi5 IP>:8443 または https://<hostname>:8443

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTNAME="${1:-$(hostname).local}"

# --------------------------------------------------------------------------
# 色付き出力
# --------------------------------------------------------------------------
_info()  { echo -e "\033[1;32m[INFO]\033[0m  $*"; }
_warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
_error() { echo -e "\033[1;31m[ERROR]\033[0m $*" >&2; }
_step()  { echo -e "\n\033[1;34m==>\033[0m $*"; }

# --------------------------------------------------------------------------
# Step 0: 依存チェック
# --------------------------------------------------------------------------
_step "依存チェック"

if ! command -v nginx &>/dev/null; then
    _error "nginx がインストールされていません。"
    echo ""
    echo "  インストール手順:"
    echo "    sudo apt update && sudo apt install -y nginx"
    echo ""
    echo "  インストール後、再度このスクリプトを実行してください:"
    echo "    bash deploy/nginx/setup.sh ${HOSTNAME}"
    exit 1
fi
_info "nginx: OK ($(nginx -v 2>&1 | head -1))"

if ! command -v openssl &>/dev/null; then
    _error "openssl がインストールされていません。"
    echo "    sudo apt install -y openssl"
    exit 1
fi
_info "openssl: OK"

# --------------------------------------------------------------------------
# Step 1: 証明書生成
# --------------------------------------------------------------------------
_step "自己署名証明書を生成 (ホスト名: ${HOSTNAME})"

CERT_FILE="/etc/ssl/agent-hub/pi5.crt"
if [[ -f "${CERT_FILE}" ]]; then
    _warn "証明書が既に存在します: ${CERT_FILE}"
    read -r -p "       再生成しますか? [y/N] " REGEN
    if [[ "${REGEN,,}" == "y" ]]; then
        bash "${SCRIPT_DIR}/gen-certs.sh" "${HOSTNAME}"
    else
        _info "証明書の再生成をスキップします。"
    fi
else
    bash "${SCRIPT_DIR}/gen-certs.sh" "${HOSTNAME}"
fi

# --------------------------------------------------------------------------
# Step 2: nginx 設定ファイルの配置
# --------------------------------------------------------------------------
_step "nginx 設定ファイルを配置"

SITES_AVAILABLE="/etc/nginx/sites-available"
SITES_ENABLED="/etc/nginx/sites-enabled"
CONF_NAME="pi5-agent-hub"

sudo cp "${SCRIPT_DIR}/pi5.conf" "${SITES_AVAILABLE}/${CONF_NAME}"
_info "設定ファイルをコピー: ${SITES_AVAILABLE}/${CONF_NAME}"

# シンボリックリンクを作成 (既存なら上書き)
sudo ln -sf "${SITES_AVAILABLE}/${CONF_NAME}" "${SITES_ENABLED}/${CONF_NAME}"
_info "シンボリックリンクを作成: ${SITES_ENABLED}/${CONF_NAME}"

# --------------------------------------------------------------------------
# Step 3: default 設定の競合チェック
# --------------------------------------------------------------------------
_step "競合チェック"

DEFAULT_ENABLED="${SITES_ENABLED}/default"
if [[ -f "${DEFAULT_ENABLED}" ]] || [[ -L "${DEFAULT_ENABLED}" ]]; then
    # default が 443 を listen しているか確認
    DEFAULT_CONF="${SITES_AVAILABLE}/default"
    if [[ -f "${DEFAULT_CONF}" ]] && grep -q "listen 443" "${DEFAULT_CONF}" 2>/dev/null; then
        _warn "default 設定が port 443 を使用しています。無効化します。"
        sudo rm -f "${DEFAULT_ENABLED}"
        _info "default を sites-enabled から削除しました。"
    else
        _info "default 設定との競合なし (port 443 未使用)。"
    fi
else
    _info "default 設定は sites-enabled に存在しません。"
fi

# --------------------------------------------------------------------------
# Step 4: nginx 設定検証 + reload
# --------------------------------------------------------------------------
_step "nginx 設定を検証して reload"

if sudo nginx -t; then
    _info "設定検証: OK"
    sudo systemctl reload nginx
    _info "nginx を reload しました。"
else
    _error "nginx 設定に問題があります。上記エラーを確認してください。"
    exit 1
fi

# --------------------------------------------------------------------------
# 完了メッセージ
# --------------------------------------------------------------------------
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "192.168.x.x")

echo ""
echo "============================================"
echo " セットアップ完了"
echo "============================================"
echo ""
echo "  アクセス URL:"
echo "    Voice (iPhone): https://${LAN_IP}"
if [[ "${HOSTNAME}" != "$(hostname).local" ]] || command -v avahi-daemon &>/dev/null; then
    echo "                   https://${HOSTNAME}"
fi
echo "    Dashboard:      https://${LAN_IP}:8443"
echo ""
echo "  iPhone に証明書をインストールしてください:"
echo "    ファイル: ${HOME:-/home/pi}/pi5.crt"
echo "    手順:     docs/https-setup.md の「iPhone 証明書インストール」を参照"
echo ""
echo "  ログ確認:"
echo "    sudo journalctl -u nginx -f"
echo "    sudo tail -f /var/log/nginx/error.log"
echo "============================================"
