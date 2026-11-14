# Some VC Utils

「Some VC Utils」は、Discord上で[VirtualCrypto](https://vcrypto.sumidora.com)を便利に使うためのBotです。
ロールのメンバーへの通貨の配布、メッセージ付きの送金、メンバーの活動への報酬を、スラッシュコマンドとWebダッシュボードから扱えます。

* [サーバーにボットを導入](https://discord.com/oauth2/authorize?client_id=1391028883868094516&permissions=68672&integration_type=0&scope=bot)
* [サポートサーバー](https://discord.gg/rqpSGFSRUH)

## コマンド

### だれでも使えるコマンド

| コマンド | 内容 |
| --- | --- |
| `/info` | Botの情報とダッシュボードのURLを表示します |
| `/rain <unit> <amount_per_user> <role>` | ロールのメンバー全員 (Botを除く) に同じ数量を配ります |
| `/send <unit> <user> <amount> <message>` | 通貨を送り、メッセージを相手のDMに届けます |
| `/settings dm <enabled>` | `/send` のメッセージをDMで受け取るかどうかを設定します |
| `/rewards` | このサーバーで受け取れる報酬を表示します |

`/rain` と `/send` は、実行した人への請求をVirtualCryptoに発行します。
VirtualCryptoで `/claim approve id:<請求ID>` を実行して承認すると、Botが相手に支払います。
返信は承認待ち、支払い中、完了と状態に合わせて更新されます。
期限 (既定で10分) までに承認されなかった請求は取り消されます。
支払えなかった相手の分は、自動的に実行した人へ返金されます。

`/send` の相手は、`/settings dm True` でメッセージの受け取りを有効にしている必要があります。

### サーバー管理者向けコマンド

`/reward_pool` は、サーバーの管理権限 (Manage Server) を持つメンバーに表示されます。

| コマンド | 内容 |
| --- | --- |
| `/reward_pool info` | 支払い元、プール残高、報酬ルール、承認待ちの発行許可を表示します |
| `/reward_pool unit <unit>` | Botのウォレットから払う報酬の通貨を設定します |
| `/reward_pool deposit <amount> [unit]` | 請求を通じて報酬プールに入金します |
| `/reward_pool withdraw <amount> <unit>` | 報酬プールから自分のウォレットに引き出します |
| `/reward_pool source <wallet\|issue>` | 報酬の支払い元を切り替えます |
| `/reward_pool rule set <reward_type> <amount> <cooldown_seconds>` | 報酬ルールを追加または更新します |
| `/reward_pool rule delete <reward_type>` | 報酬ルールを削除します |

報酬の種類は `message` (メッセージの送信) と `server_join` (サーバーへの参加) です。
同じメンバーが同じ種類の報酬を次に受け取れるのは、`cooldown_seconds` が経ってからです。

## 報酬の支払い元

| 支払い元 | 仕組み |
| --- | --- |
| `wallet` (Botのウォレット) | 管理者が請求で入金した通貨をBotのウォレットで預かり、サーバーごとのプール残高から報酬を払います。残高が足りない間は報酬を払いません |
| `issue` (サーバーの発行枠) | サーバーの通貨の発行枠から報酬を直接発行します。入金は要りません |

`issue` に切り替えると、Botが発行の許可を申請し、承認用のコードを表示します。
サーバーの管理者がVirtualCryptoで `/grant approve code:<コード>` を実行すると切り替わり、結果がDMで届きます。
許可はVirtualCryptoの `/grant server` からいつでも取り消せます。取り消すと報酬は止まります。

## ダッシュボード

`/info` に表示されるURLから、Discordアカウントでログインできます。

* サーバーの管理権限を持つサーバーについて、支払い元、報酬の通貨、報酬ルール、入金と引き出しを設定し、操作と支払いの履歴、プールの記録を確認できます。
* 個人設定で、`/send` のメッセージの受け取りを切り替え、自分の操作の履歴を確認できます。
* Botの運営者は、結果が確定しない支払いの確認と、VirtualCryptoとの連携状態の確認ができます。

## 仕組み

Botは1つのDockerイメージを役割ごとに動かし、Cloudflare Tunnelを通じてVirtualCryptoのWebhookとダッシュボードを公開します。
すべての支払いはデータベースに記録してから送金し、VirtualCryptoの冪等キーを付けて送るため、再送しても二重に支払われません。
詳しくは次の文書を参照してください。

* [運用ガイド](document/operations.md): 配備、設定、更新、バックアップ、旧版からの移行
* [設計](document/architecture.md): 操作の状態遷移、支払いのoutbox、報酬、Webhook
* [ダッシュボード](document/dashboard.md): ログインと権限、画面

## 開発

Python 3.12 で開発します。
結合テストは使い捨てのPostgreSQLコンテナを起動するため、Dockerが必要です (`SVU_TEST_DB_URL` で既存のデータベースも指定できます。名前は `_test` で終わる必要があります)。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -c constraints.txt -e '.[dev]'
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest
```
