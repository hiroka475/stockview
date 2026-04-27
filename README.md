# StockView

無料で使える株価チャート＆ファンダメンタルズ分析ツール（日本株・米国株対応）。

- 株価チャート（日足・週足、最大15年分）
- テクニカル指標（EMA・ボリンジャーバンド）
- ファンダメンタルズ分析（IR BANK連携）
- ウォッチリスト・株価アラート（無制限）
- トレンドライン描画

データソース: [Yahoo Finance](https://finance.yahoo.com/) / [IR BANK](https://irbank.net/)

---

## ⚠️ 利用上の注意

- 本ツールは投資助言を行うものではありません。投資判断はご自身の責任でお願いします。
- 株価データは約20分の遅延があります。デイトレード用途には適していません。
- 中長期投資のチャート分析・銘柄管理ツールとしてお使いください。

---

## 動作環境

- **Node.js 20 以上**（[公式サイト](https://nodejs.org/) からインストール）
- Mac / Windows / Linux いずれもOK
- ブラウザは Chrome / Edge / Safari など最新版を推奨

---

## ローカルで動かす（開発・自分専用ツールとして）

### 1. リポジトリを取得

```bash
git clone https://github.com/hiroka475/stockview.git
cd stockview
```

### 2. 依存パッケージをインストール

```bash
npm install
```

初回は数分かかります（数百MBのダウンロードあり）。

### 3. 開発サーバーを起動

```bash
npm run dev
```

ブラウザで `http://localhost:3000` を開けば StockView が使えます。

### 4. 終了

ターミナルで `Ctrl + C` を押すと停止します。

### 2回目以降の起動

```bash
cd stockview
npm run dev
```

---

## 自分の Cloudflare Workers にデプロイする

ローカル版に慣れたら、自分専用の URL でアクセスできるよう Cloudflare Workers にデプロイすることもできます。スマホやタブレットからもアクセス可能になります。
詳細手順は [DEPLOY.md](DEPLOY.md) を参照してください。

ざっくりの流れ:

1. Cloudflare アカウントを作成（無料プランでOK）
2. `npx wrangler kv namespace create "KV"` で KV namespace を作成
3. `wrangler.jsonc` の `id` / `preview_id` を自分の値に書き換え
4. `npm run deploy` でデプロイ
5. Cloudflare Access で自分のメアドだけ許可

---

## 主要技術スタック

- フロントエンド: Vanilla JS + HTML（[`public/index.html`](public/index.html)）
- フレームワーク: [Next.js 15](https://nextjs.org/)（API Routes 用）
- ホスティング: [Cloudflare Workers](https://workers.cloudflare.com/) + [OpenNext](https://opennext.js.org/cloudflare)
- データソース: Yahoo Finance（株価）/ IR BANK（ファンダ）

---

## ライセンス

[MIT](LICENSE)

---

## 免責事項

- 本ツールの利用によって生じたいかなる損害についても、開発者は責任を負いません。
- API 提供元（Yahoo Finance / IR BANK）の利用規約に従ってお使いください。
- 過度なリクエストを送るような改変はしないでください。
