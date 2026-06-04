# HTTPS セットアップガイド — Pi5 nginx reverse proxy

voice-gateway を iPhone から使用するには **HTTPS が必須** です（iOS Safari の `getUserMedia()` は secure context 以外で動作しません）。本ガイドでは nginx reverse proxy と自己署名証明書を使って Pi5 上の全サービスを HTTPS で提供する手順を説明します。

> **対応 issue**: [kishibashi3/agent-hub-voice#1](https://github.com/kishibashi3/agent-hub-voice/issues/1)

---

## サービス構成

| サービス | 内部ポート | HTTPS URL | 用途 |
|---|---|---|---|
| voice-gateway | :8765 | `https://<Pi5 IP>` (443) | iPhone 音声 UI |
| agent-hub-dashboard | :8080 | `https://<Pi5 IP>:8443` | 管理ダッシュボード |

nginx が SSL 終端を行い、各サービスに HTTP でプロキシします。

```
iPhone/ブラウザ
  │  wss://<Pi5 IP>/ws   (port 443)
  ▼
nginx (Pi5)
  ├── :443  → voice-gateway    :8765
  └── :8443 → agent-hub-dashboard :8080
```

---

## 1. 前提条件

Pi5 上で以下がインストール済みであること:

```bash
sudo apt update
sudo apt install -y nginx openssl
```

agent-hub-voice リポジトリがクローン済みであること:

```bash
# 例: /home/pi/agent-hub-voice に clone している場合
git clone https://github.com/kishibashi3/agent-hub-voice.git /home/pi/agent-hub-voice
```

---

## 2. クイックスタート（ワンコマンド）

Pi5 で以下を実行するだけでセットアップが完了します:

```bash
cd /home/pi/agent-hub-voice
bash deploy/nginx/setup.sh [Pi5のホスト名.local]
```

**例:**
```bash
# hostname が "raspberrypi" の場合
bash deploy/nginx/setup.sh raspberrypi.local

# Pi5 のホスト名が "pi5" の場合
bash deploy/nginx/setup.sh pi5.local
```

ホスト名を省略すると `$(hostname).local` が自動的に使われます。

スクリプトが行うこと:
1. 自己署名証明書を生成 (`/etc/ssl/agent-hub/pi5.crt`)
2. nginx 設定を `sites-available/pi5-agent-hub` に配置
3. `sites-enabled` にシンボリックリンクを作成
4. nginx を reload

---

## 3. 手動セットアップ（ステップごと）

### 3-1. 証明書の生成

```bash
cd /home/pi/agent-hub-voice
bash deploy/nginx/gen-certs.sh raspberrypi.local
```

生成ファイル:
- `/etc/ssl/agent-hub/pi5.crt` — 証明書（iPhone にインストールするファイル）
- `/etc/ssl/agent-hub/pi5.key` — 秘密鍵
- `~/pi5.crt` — iPhone 転送用コピー

> **重要**: スクリプト実行後に表示される **SHA-256 フィンガープリント** をメモしてください。iPhone での証明書インストール時に照合します。

### 3-2. nginx 設定の配置

```bash
sudo cp deploy/nginx/pi5.conf /etc/nginx/sites-available/pi5-agent-hub
sudo ln -sf /etc/nginx/sites-available/pi5-agent-hub \
            /etc/nginx/sites-enabled/pi5-agent-hub
```

### 3-3. default 設定との競合解消（必要な場合）

nginx の default 設定が port 443 を使用している場合は無効化します:

```bash
# default の設定内容を確認
grep "listen" /etc/nginx/sites-enabled/default 2>/dev/null

# 443 を使用している場合は無効化
sudo rm /etc/nginx/sites-enabled/default
```

### 3-4. 設定検証と reload

```bash
sudo nginx -t           # 設定ファイルの文法チェック
sudo systemctl reload nginx
```

---

## 4. iPhone への証明書インストール

自己署名証明書を iPhone に「信頼済み証明書」として登録する手順です。

### 4-1. 証明書ファイルを iPhone に転送

Pi5 から iPhone に `pi5.crt` を転送します。いずれかの方法を選択してください:

**方法 A: メール添付**
```bash
# Pi5 から自分のメールアドレスに送信（mutt / sendmail が使える場合）
mutt -s "Pi5 証明書" -a ~/pi5.crt -- your@email.com < /dev/null

# または Gmail/Outlook 等でメール作成 → ~/pi5.crt を添付して自分に送信
```

**方法 B: HTTP サーバー経由**
```bash
# Pi5 で一時的に HTTP サーバーを起動
cd ~ && python3 -m http.server 9999

# iPhone Safari で以下にアクセス
# http://<Pi5 の LAN IP>:9999/pi5.crt
```

**方法 C: AirDrop（Mac 経由）**
```bash
# まず Mac に scp でコピー
scp pi@raspberrypi.local:~/pi5.crt ~/Desktop/

# Mac の AirDrop で iPhone に送信
```

### 4-2. 証明書プロファイルをインストール

1. iPhone で `pi5.crt` を開く（メールの添付ファイルをタップ、または Safari でダウンロード）
2. 「プロファイルがダウンロードされました」と表示されたら **「閉じる」** をタップ
3. **設定** アプリを開く
4. 上部に「プロファイルがダウンロードされました」が表示されているのでタップ
5. 右上の **「インストール」** をタップ
6. パスコードを入力
7. 警告画面で **「インストール」** をタップ
8. **「完了」** をタップ

### 4-3. 証明書を「信頼」に設定

インストールだけでは不十分です。**ルート証明書として信頼**する手順が必要です。

1. **設定** → **一般** → **情報** → **証明書信頼設定** を開く
2. 「agent-hub Pi5」の証明書が表示されている
3. トグルを **オン** にする
4. 確認ダイアログで **「続ける」** をタップ

> ⚠️ この設定をしないと Safari でアクセスしても「この接続はプライベートではありません」エラーになります。

### 4-4. 接続確認

iPhone の Safari で以下にアクセスして、アドレスバーに鍵アイコンが表示されることを確認します:

```
https://<Pi5 の LAN IP>
```

または mDNS ホスト名で:
```
https://raspberrypi.local
```

> **フィンガープリント照合**: Safari でサイト情報 → 「証明書」を確認すると SHA-256 フィンガープリントが表示されます。`gen-certs.sh` 実行時に出力されたフィンガープリントと一致することを確認してください。

---

## 5. 動作確認

### Pi5 側

```bash
# nginx の状態確認
sudo systemctl status nginx

# ポートの確認
sudo ss -tlnp | grep nginx

# アクセスログの確認
sudo tail -f /var/log/nginx/access.log

# エラーログの確認
sudo tail -f /var/log/nginx/error.log
```

### エンドポイント確認

```bash
# voice-gateway health チェック (Pi5 ローカル)
curl -k https://localhost/health
# → {"status":"ok","session_active":false}

# dashboard (Pi5 ローカル)
curl -k https://localhost:8443/health
```

> `-k` オプションは自己署名証明書の検証をスキップします（Pi5 ローカルからの確認時のみ使用）。

---

## 6. 証明書の更新

証明書の有効期限（825日）が近づいたら更新します:

```bash
cd /home/pi/agent-hub-voice
bash deploy/nginx/gen-certs.sh raspberrypi.local
sudo systemctl reload nginx
```

その後、iPhone に新しい証明書を再インストールします（§4 の手順を再実行）。

---

## 7. トラブルシューティング

### Safari で「この接続はプライベートではありません」

**原因**: 証明書が iPhone で信頼されていない  
**対処**: §4-3「証明書を信頼に設定」の手順を実施

### Safari で「接続できませんでした」

**確認事項**:
1. Pi5 と iPhone が同一 LAN に接続しているか
2. nginx が起動しているか: `sudo systemctl status nginx`
3. voice-gateway / dashboard が起動しているか: `sudo systemctl status voice-gateway`
4. Pi5 のファイアウォールが 443 / 8443 を許可しているか:
   ```bash
   sudo ufw status
   # 必要なら:
   sudo ufw allow 443/tcp
   sudo ufw allow 8443/tcp
   ```

### mDNS ホスト名 (raspberrypi.local) でアクセスできない

**確認事項**:
1. Pi5 で Avahi が起動しているか: `sudo systemctl status avahi-daemon`
2. 起動していない場合: `sudo apt install -y avahi-daemon && sudo systemctl enable --now avahi-daemon`
3. iPhone が `.local` 解決できない場合は LAN IP アドレスで直接アクセス

### nginx の設定エラー

```bash
sudo nginx -t    # エラー内容を確認
sudo journalctl -u nginx -n 50   # 起動ログ確認
```

---

## 8. ファイル一覧

```
deploy/nginx/
├── pi5.conf       nginx vhost 設定 (voice-gateway + dashboard)
├── gen-certs.sh   自己署名証明書の生成スクリプト
└── setup.sh       ワンショットセットアップスクリプト

docs/
└── https-setup.md  本ドキュメント
```

---

## 9. 参照

- [issue #1](https://github.com/kishibashi3/agent-hub-voice/issues/1) — HTTPS サポート要件
- [voice-gateway 設計](https://github.com/kishibashi3/agent-hub/blob/main/docs/voice-gateway.md) — voice-gateway の全体設計
- [Pi5 Deployment Guide](https://github.com/kishibashi3/agent-hub/blob/main/docs/deployment-pi5.md) — Pi5 デプロイメント全体像
