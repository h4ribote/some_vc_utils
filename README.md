# Some VC Utils

「Some VC Utils」は、Discord上で[VirtualCrypto](https://vcrypto.sumidora.com)を簡単に利用するためのユーティリティボットです。
特定のロールを持つメンバーへの通貨の配布や、メッセージ付きの送金など、VirtualCryptoをより便利に活用するための機能を提供します。

*   [サーバーにボットを導入](https://discord.com/oauth2/authorize?client_id=1391028883868094516&permissions=68672&integration_type=0&scope=bot)
*   [サポートサーバー](https://discord.gg/rqpSGFSRUH)

## コマンド

### 一般コマンド

*   `/info`: ボットに関する情報を表示します。
*   `/rain <unit> <amount_per_user> <role>`: 指定したロールを持つすべてのメンバーに、指定された量の通貨を配布します。
*   `/send_with_msg <unit> <user> <amount> <message>`: ダイレクトメッセージと共にユーザーに通貨を送信します。
*   `/receive_msg <True|False>`: トランザクションに関するダイレクトメッセージの受信を有効または無効にします。

### 報酬プールコマンド

*   `/reward_pool init <unit>`: 報酬プールを特定の通貨単位で初期化します。（管理者のみ）
*   `/reward_pool set <reward_type> <amount> <cooldown_seconds>`: 特定のアクションに対する報酬ルールを設定します。（管理者のみ）
*   `/reward_pool delete <reward_type>`: 報酬ルールを削除します。（管理者のみ）
*   `/reward_pool info`: 報酬プールの現在のステータスとルールを表示します。
*   `/reward_pool deposit <amount>`: 報酬プールに通貨を入金します。（管理者のみ）

## 主な機能

*   **通貨の配布 (Rain):** 指定したロールを持つすべてのメンバーに通貨を均等に配布します。
*   **メッセージ付き送金:** 他のユーザーにパーソナライズされたダイレクトメッセージを添えて通貨を送金します。
*   **DM受信設定:** ユーザーはトランザクションに関するDM通知を受け取るかどうかを選択できます。
*   **発言報酬:** サーバーでのメッセージ送信に対してユーザーに報酬を与えます。
*   **参加報酬:** サーバーへの参加に対してユーザーに報酬を与えます。

## 追加予定

*   追加の報酬トリガー（例: リアクション、ボイスチャンネルでの活動）。
*   設定を簡単にするためのWebダッシュボード。

## 導入方法

1.  リポジトリをクローンします:
    ```bash
    git clone https://github.com/h4ribote/some_vc_utils.git
    cd some_vc_utils
    ```

2.  依存関係をインストールします:
    ```bash
    pip install discord.py aiohttp
    ```

3.  設定ファイル `config.py` を作成します:
    ```bash
    cp config.py.example config.py
    ```

4.  `config.py` を編集して、ボットの認証情報を入力します:
    *   `VirtualCrypto`:
        *   `client_id`: VirtualCryptoのクライアントID
        *   `client_secret`: VirtualCryptoのクライアントシークレット
        *   `public_key`: VirtualCryptoの公開鍵
    *   `Discord`:
        *   `BOT_TOKEN`: Discordボットのトークン
        *   `ADMIN`: 管理者コマンドの使用を許可するユーザーIDのリスト

3.  ボットの実行:
    ```bash
    python3 main.py
    ```

## データベース

このボットは、報酬設定やユーザーのクールダウン情報を保存するためにSQLiteデータベースを使用します。データベースのスキーマは `db_structs.py` で定義されており、以下のテーブルで構成されています:

*   **`RewardPool`**: 各サーバーの報酬プールの残高を保存します。
    *   `guild_id`: サーバーのID。
    *   `unit`: 報酬プールの通貨単位。
    *   `pool_balance`: 報酬プールの現在の残高。
*   **`RewardConfig`**: 各サーバーの報酬設定を保存します。
    *   `config_id`: 設定の一意なID。
    *   `guild_id`: サーバーのID。
    *   `reward_type`: 報酬をトリガーするイベントの種類（例: `message`、`server_join`）。
    *   `amount`: 報酬として付与する通貨の量。
    *   `cooldown_seconds`: ユーザーが同じ種類の報酬を再度受け取るまでのクールダウン期間（秒）。
*   **`UserRewardCooldown`**: クールダウンを適用するために、ユーザーが最後に報酬を受け取った時刻を記録します。
    *   `user_id`: ユーザーのID。
    *   `guild_id`: サーバーのID。
    *   `reward_type`: 報酬の種類。
    *   `last_triggered_timestamp`: ユーザーが最後に報酬を受け取ったUNIXタイムスタンプ。
