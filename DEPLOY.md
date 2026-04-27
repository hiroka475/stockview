# StockView Cloudflare デプロイ手順

自分のCloudflareアカウントにデプロイすれば、スマホ・タブレット・他のPCからもアクセスできるようになります。Cloudflare Access で自分のメールアドレスだけを許可することで、他人は使えない自分専用版になります。

## 前提条件
- Cloudflare アカウント(無料プランでOK)
- Node.js 20 以上がインストールされていること
- このリポジトリを `git clone` 済みで `npm install` まで完了していること

---

## 1. Wrangler にログイン

```bash
npx wrangler login
```

ブラウザが自動で開くので、Cloudflare アカウントでログインしてください。

---

## 2. KV ネームスペースを作成

ウォッチリストやキャッシュデータを保存する「KVストレージ」を Cloudflare に作ります。

```bash
# 本番用
npx wrangler kv namespace create "KV"
# プレビュー用(ローカル開発)
npx wrangler kv namespace create "KV" --preview
```

それぞれのコマンドを実行すると、以下のような出力が出ます:

```
{ id: "abcdef1234567890abcdef1234567890" }
```

この **id** を `wrangler.jsonc` の該当箇所(`REPLACE_WITH_YOUR_KV_ID` と `REPLACE_WITH_YOUR_KV_PREVIEW_ID`)に貼り付けます:

```jsonc
"kv_namespaces": [
  {
    "binding": "KV",
    "id": "ここに本番のidを貼り付け",
    "preview_id": "ここにpreviewのidを貼り付け"
  }
]
```

---

## 3. ビルドとデプロイ

```bash
npm run deploy
```

デプロイが成功すると、以下のような URL が表示されます:

```
https://stockview.<あなたのアカウント名>.workers.dev
```

このURLが、あなた専用のStockViewのアドレスになります。

---

## 4. Cloudflare Access で認証を設定(重要)

このままでは新しいURLは認証なしでアクセスできてしまいます。**自分以外がアクセスできないようにする**ために、Cloudflare Access を設定します。

1. **Cloudflare Zero Trust ダッシュボード** を開く: https://one.dash.cloudflare.com/
2. 左メニュー「Access」→「Applications」
3. 「Add an application」→「Self-hosted and private」を選択して「Continue」
4. 以下を設定:
   - **Application name**: `StockView`(任意)
   - **Application domain**:
     - Subdomain: `stockview`(デプロイ時のWorker名)
     - Domain: ドロップダウンから `<あなたのアカウント名>.workers.dev` を選択
5. 次の画面でポリシーを追加:
   - **Policy name**: `自分だけ許可`
   - **Action**: `Allow`
   - **Include**:
     - Selector: `Emails`
     - Value: `your-email@example.com`(あなた自身のメールアドレス)
6. 「Add application」で保存

これで **あなたのメールアドレスでログインした人だけ** がアクセスできるようになります。
他のメールでアクセスしようとすると、認証画面で弾かれます。

---

## 5. WEBHOOK_SECRET を設定(任意)

スクリーニングデータを外部から更新する場合のみ必要です。普段は不要です。

```bash
npx wrangler secret put WEBHOOK_SECRET
# 入力プロンプトが出たら、任意のランダム文字列を入力
```

---

## ローカル開発(自分のPCで動かす)

```bash
npm run dev
# http://localhost:3000 でアクセス可能
```

ローカルでは KV の代わりにメモリキャッシュを使用するので、ブラウザを閉じるとデータは消えます。

Cloudflare Workers環境(KV含む)をローカルでシミュレートする場合:

```bash
npm run preview
# Workers環境で動作確認できる
```

---

## アプリの URL 構成

| URL | 内容 |
|-----|------|
| `/index.html` | 株価チャート(メイン) |
| `/fundamentals.html` | ファンダメンタルズ分析 |
| `/screening.html` | スクリーニング |
| `/api/stock/7203` | 株価データ API |
| `/api/dividend/7203.T` | 配当データ API |
| `/api/fundamentals/7203` | ファンダメンタルズ API |
