#!/usr/bin/env bash
# gen-certs.sh — Pi5 向け自己署名証明書を生成する
#
# Usage:
#   bash deploy/nginx/gen-certs.sh [hostname]
#
# 引数:
#   hostname  Pi5 の mDNS ホスト名 (デフォルト: $(hostname).local)
#             例: raspberrypi.local / pi5.local
#
# 出力:
#   /etc/ssl/agent-hub/pi5.crt  — 証明書 (iPhone にインストールするファイル)
#   /etc/ssl/agent-hub/pi5.key  — 秘密鍵
#
# 証明書の SAN (Subject Alternative Names):
#   - 指定ホスト名 と *.ホスト名
#   - Pi5 の LAN IP アドレス (自動検出)
#   - localhost / 127.0.0.1
#
# iOS 要件:
#   - RSA 2048bit 以上 (本スクリプトは 2048bit を使用)
#   - 有効期限 825日以内 (iOS 13+ の制限)
#   - SAN 必須 (iOS は CN を証明書検証に使用しない)

set -euo pipefail

# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------
HOSTNAME="${1:-$(hostname).local}"
CERT_DIR="/etc/ssl/agent-hub"
CERT_FILE="${CERT_DIR}/pi5.crt"
KEY_FILE="${CERT_DIR}/pi5.key"
DAYS=825          # iOS 上限 (iOS 13 以降は 825 日超の証明書を拒否)
KEY_BITS=2048

# Pi5 の LAN IP を自動検出 (eth0 / wlan0 の最初の IP)
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [[ -z "${LAN_IP}" ]]; then
    LAN_IP="127.0.0.1"
    echo "[warn] LAN IP を検出できませんでした。127.0.0.1 を使用します。"
fi

# --------------------------------------------------------------------------
# 前処理
# --------------------------------------------------------------------------
echo "============================================"
echo " agent-hub Pi5 自己署名証明書生成"
echo "============================================"
echo "  ホスト名 : ${HOSTNAME}"
echo "  LAN IP  : ${LAN_IP}"
echo "  有効期限 : ${DAYS} 日"
echo "  出力先   : ${CERT_DIR}"
echo "============================================"
echo ""

# 出力ディレクトリを作成 (chmod 700 で一時的な world-readable ウィンドウを排除)
sudo mkdir -p "${CERT_DIR}"
sudo chmod 700 "${CERT_DIR}"

# --------------------------------------------------------------------------
# OpenSSL 設定ファイルを一時作成 (SAN を含む)
# --------------------------------------------------------------------------
OPENSSL_CNF=$(mktemp /tmp/pi5-cert-XXXXXX.cnf)
trap 'rm -f "${OPENSSL_CNF}"' EXIT

cat > "${OPENSSL_CNF}" <<CONF
[req]
distinguished_name = req_distinguished_name
x509_extensions    = v3_req
prompt             = no

[req_distinguished_name]
C  = JP
ST = Japan
O  = agent-hub Pi5
CN = ${HOSTNAME}

[v3_req]
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid,issuer
# CA:FALSE — TLS サーバー証明書として生成 (CA 証明書ではない)
# CA:TRUE にすると秘密鍵漏洩時に任意ドメインの証明書偽造が可能になるため不可
# iOS への自己署名証明書インストールは CA:FALSE でも同様に機能する
basicConstraints       = critical, CA:FALSE
keyUsage               = critical, digitalSignature, keyEncipherment
extendedKeyUsage       = serverAuth
subjectAltName         = @alt_names

[alt_names]
DNS.1 = ${HOSTNAME}
DNS.2 = *.${HOSTNAME}
DNS.3 = localhost
IP.1  = 127.0.0.1
IP.2  = ${LAN_IP}
CONF

# --------------------------------------------------------------------------
# 証明書生成
# --------------------------------------------------------------------------
echo "証明書を生成しています..."
sudo openssl req \
    -x509 \
    -newkey "rsa:${KEY_BITS}" \
    -nodes \
    -keyout "${KEY_FILE}" \
    -out    "${CERT_FILE}" \
    -days   "${DAYS}" \
    -config "${OPENSSL_CNF}" \
    2>&1

# --------------------------------------------------------------------------
# パーミッション設定
# --------------------------------------------------------------------------
sudo chmod 600 "${KEY_FILE}"   # 秘密鍵は root のみ読み取り可
sudo chmod 644 "${CERT_FILE}"  # 証明書は全員読み取り可 (nginx が読む)

# --------------------------------------------------------------------------
# 結果確認
# --------------------------------------------------------------------------
echo ""
echo "============================================"
echo " 証明書生成完了"
echo "============================================"
echo "  証明書 : ${CERT_FILE}"
echo "  秘密鍵 : ${KEY_FILE}"
echo ""

echo "--- SAN (Subject Alternative Names) ---"
openssl x509 -in "${CERT_FILE}" -noout -ext subjectAltName 2>/dev/null || \
    openssl x509 -in "${CERT_FILE}" -noout -text | grep -A1 "Subject Alternative Name"
echo ""

echo "--- SHA-256 フィンガープリント (iPhone インストール時の照合用) ---"
openssl x509 -in "${CERT_FILE}" -noout -fingerprint -sha256
echo ""

# iPhone 向け: .crt を /home/pi にコピー (メール添付 or HTTP 転送で iPhone に送る)
PI_HOME="${HOME:-/home/pi}"
EXPORT_PATH="${PI_HOME}/pi5.crt"
sudo cp "${CERT_FILE}" "${EXPORT_PATH}"
sudo chown "$(id -u):$(id -g)" "${EXPORT_PATH}"
echo "--- iPhone インストール用ファイル ---"
echo "  ${EXPORT_PATH}"
echo "  (iPhone へのインストール手順は docs/https-setup.md を参照)"
echo ""
echo "============================================"
echo " 次のステップ"
echo "============================================"
echo "  1. nginx セットアップ (未実施の場合):"
echo "     bash deploy/nginx/setup.sh"
echo ""
echo "  2. iPhone に証明書をインストール:"
echo "     docs/https-setup.md の「iPhone 証明書インストール」を参照"
echo ""
echo "  3. 接続確認:"
echo "     Voice:     https://${LAN_IP}"
echo "     Dashboard: https://${LAN_IP}:8443"
echo "============================================"
