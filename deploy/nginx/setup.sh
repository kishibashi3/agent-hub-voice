#!/usr/bin/env bash
# setup.sh — Pi5 nginx HTTPS プロキシ ワンショットセットアップ
#
# 実行すること:
#   1. nginx / openssl がインストール済みか確認 (未インストールならガイドを表示)
#   2. 自己署名証明書を生成 (gen-certs.sh を呼び出す)
#   3. websocket_map.conf を /etc/nginx/conf.d/ に配置
#   4. nginx 設定ファイルを sites-available に配置
#   5. sites-enabled にシンボリックリンクを作成
#   6. port 443/8443 と競合する既存設定を全走査して無効化
#   7. nginx の設定を検証して reload
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
# Step 2: WebSocket map を conf.d に配置
# --------------------------------------------------------------------------
_step "WebSocket upgrade map を配置"

CONFD="/etc/nginx/conf.d"
sudo cp "${SCRIPT_DIR}/websocket_map.conf" "${CONFD}/websocket_map.conf"
_info "WebSocket map を配置: ${CONFD}/websocket_map.conf"

# --------------------------------------------------------------------------
# Step 3: nginx 設定ファイルの配置
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
# Step 4: port 443/8443 競合チェック
# シンボリックリンク環境でも実ファイルを参照して全設定を走査する
# --------------------------------------------------------------------------
_step "競合チェック"

conflict_found=false
for enabled_conf in "${SITES_ENABLED}"/*; do
    # 今設置した自分の設定はスキップ
    [[ "$(basename "${enabled_conf}")" == "${CONF_NAME}" ]] && continue
    # 存在しないエントリをスキップ (glob が空の場合)
    [[ -e "${enabled_conf}" ]] || continue

    # シンボリックリンクの場合は実体ファイルの内容を確認
    real_conf=$(realpath "${enabled_conf}" 2>/dev/null || echo "${enabled_conf}")

    if grep -qE "listen[[:space:]]+((\[::]:)?(443|8443)|443[[:space:]]|8443[[:space:]])" \
            "${real_conf}" 2>/dev/null; then
        _warn "port 443/8443 と競合する設定を無効化します: ${enabled_conf}"
        sudo rm -f "${enabled_conf}"
        _info "削除: ${enabled_conf}"
        conflict_found=true
    fi
done

if [[ "${conflict_found}" == "false" ]]; then
    _info "競合する設定なし。"
fi

# --------------------------------------------------------------------------
# Step 5: nginx 設定検証 + reload
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
