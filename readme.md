# GOGTubeもどき - GOGTube-modoki

YouTubeの動画をダウンロードし、MP3などの形式に変換するためのWebアプリケーション、およびバックエンドエンジンです。 

注意：このソフトウェアは教育・研究目的で開発されており、使用する際には著作権法をはじめとした法律を厳守してください。その他の法的情報はLICENSEファイルをご覧ください。


## 主な特徴

- **シンプルなWebインターフェース**: URLを貼り付けるだけで、誰でも簡単に動画や音声をダウンロードできます。
- **YouTube検索機能**: アプリケーション上部の検索バーから直接YouTube動画を検索し、結果からすぐにダウンロードを開始できます。
- **多彩なダウンロードオプション**:
    - **動画/音声の選択**: 完全な動画ファイル、または音声のみを抽出してダウンロードするかを選択できます。
    - **コーデック指定**: 用途に合わせて、互換性の高いフォーマットやデータ効率の良いフォーマットを指定できます。
    - **直接再生機能**: 動画をサーバーにダウンロードせずに、Googleのサーバーから直接ストリーミング再生・ダウンロードするオプションです。すぐに内容を確認したい場合に便利です。
- **リアルタイムな進捗確認**: 各ダウンロードリクエストには専用のステータスページが用意され、キューの順番待ち、ダウンロードの進捗(%)、完了、失敗といった状況をリアルタイムで確認できます。
- **管理者向けダッシュボード**: パスワードで保護された管理ページで、以下の操作が可能です。
    - 全てのダウンロードリクエストの監視と管理
    - ストレージ使用量の確認と上限設定
    - リクエスト者のIPアドレス/ユーザー名確認
    - 個別ファイルの削除や、ダウンロードの優先順位の変更
- **PWA対応**: スマートフォンやPCにアプリとしてインストールして、素早くアクセスできます。

---

## 1. Dockerユーザー向けガイド

すぐにアプリケーションとして利用したい方はこちらを参照してください。

### 前提条件

- Docker
- Docker Compose

### 使い方

1.  **リポジトリをクローンします:**
    ```bash
    git clone https://github.com/your-username/ytmp3modoki2.git
    cd ytmp3modoki2
    ```

2.  **`docker-compose.yml`を作成します:**
    `docker-compose.example.yml` を `docker-compose.yml` にコピーし、必要に応じて環境変数を編集してください。
    ```bash
    cp docker-compose.example.yml docker-compose.yml
    ```

3.  **アプリケーションをビルドして実行します:**
    ```bash
    docker-compose up -d --build
    ```

4.  **アプリケーションにアクセスします:**
    ウェブブラウザを開き、 `http://localhost:8080` にアクセスしてください。

### 環境変数 (オプション)

`docker-compose.yml` の `environment` セクションで、アプリケーションの動作をカスタマイズできます。

#### **基本設定**
| 環境変数 | 説明 | デフォルト値 |
| :--- | :--- | :--- |
| `ADMIN_LOGIN` | 管理者ページのログインユーザー名 | `admin` |
| `ADMIN_PASSWORD` | 管理者ページのログインパスワード | `password` |
| `ADMIN_DEBUG` | デバッグモードを有効にする | `False` |

#### **ダウンロード設定**
| 環境変数 | 説明 | デフォルト値 |
| :--- | :--- | :--- |
| `DOWNLOAD_BIND_IP` | ダウンロード時に使用するIPアドレス | `0.0.0.0` |
| `DOWNLOAD_THREADS` | 同時ダウンロード数 | `2` |
| `ADMIN_NEW_REQUEST`| 新しいダウンロードリクエストを許可するか | `True` |

#### **ストレージ設定**
| 環境変数 | 説明 | デフォルト値 |
| :--- | :--- | :--- |
| `STORAGE_LIMIT_SIZE` | ファイルを保存するストレージの上限サイズ (MB) | `3000` |
| `STORAGE_AUTO_DELETE`| 上限到達時に古いファイルから自動削除するか | `True` |

#### **Google OAuth 設定 (管理者ページ)**
| 環境変数 | 説明 | デフォルト値 |
| :--- | :--- | :--- |
| `ADMIN_GOOGLE_OAUTH` | Google OAuth認証を有効にする | `False` |
| `GOOGLE_OAUTH_CLIENT_ID` | **(必須)** Google Cloud発行のクライアントID | (なし) |
| `GOOGLE_OAUTH_PROJECT_ID`| **(必須)** Google CloudのプロジェクトID | (なし) |
| `GOOGLE_OAUTH_CLIENT_SECRET`| **(必須)** Google Cloud発行のクライアントシークレット | (なし) |
| `GOOGLE_OAUTH_REDIRECT_URI`| Google Cloudで設定したリダイレクトURI | `https://yt.gog-lab.org/google-callback` |
| `GOOGLE_OAUTH_AUTH_URI`| (上級者向け) 認証URI | `https://accounts.google.com/o/oauth2/auth` |
| `GOOGLE_OAUTH_TOKEN_URI`| (上級者向け) トークンURI | `https://oauth2.googleapis.com/token` |

---

## 2. 開発者向けガイド (`core.py`の利用)

`app/core.py` をバックエンドエンジンとしてインポートし、独自のフロントエンドやシステムを構築したい方向けのガイドです。

### `core.py`とは？

`core.py`は、`yt-dlp`をラップし、以下の機能を提供するバックエンドエンジンです。

- **リクエスト管理**: 動画のダウンロードリクエストをキューに追加し、管理します。
- **マルチスレッドダウンロード**: 複数のダウンロードを並行して処理します。
- **ステータス追跡**: 各リクエストの状況（キューイング、ダウンロード中、完了、失敗）を追跡します。
- **ストレージ管理**: 保存ファイルの合計サイズを監視し、設定した上限を超えた場合に古いファイルから自動的に削除します。
- **YouTube検索**: キーワードによる動画検索機能を提供します。

### 基本的な使い方

`yt_modoki2`クラスをインスタンス化し、各種メソッドを呼び出すことで利用します。

#### 1. インスタンスの作成

```python
from app.core import yt_modoki2

# coreエンジンのインスタンスを作成
core = yt_modoki2()
```

#### 2. ダウンロードリクエスト

`new_request()`メソッドで新しいダウンロードをキューに追加します。リクエストが受け付けられると、一意の`uuid`が返されます。

```python
video_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# 音声のみをダウンロードするリクエスト
uuid, is_duplicate = core.new_request(
    url=video_url,
    is_video=False, # Falseで音声のみ
    user="my-app" # 識別のためのユーザー名を指定
)

if is_duplicate:
    print(f"同じリクエストが既に存在します。UUID: {uuid}")
else:
    print(f"リクエストをキューに追加しました。UUID: {uuid}")
```

#### 3. ステータスの確認

`video_dic`辞書に、`uuid`をキーとして動画の情報 (`video_item`オブジェクト) が格納されています。`status`属性を確認することで、現在の状態を知ることができます。

```python
import time
from glob import glob

while True:
    video_item = core.video_dic.get(uuid)

    if not video_item:
        print("リクエストが見つかりません。")
        break

    status = video_item.status
    progress = video_item.downloaded_percent

    print(f"[{uuid}] 現在のステータス: {status}, 進捗: {progress}%")

    if status == "completed":
        print("処理が完了しました。")
        # ファイルは outputs/<uuid>/output.* に保存されます
        try:
            file_path = glob(f"outputs/{uuid}/output.*")[0]
            print(f"ファイルパス: {file_path}")
        except IndexError:
            print("ファイルが見つかりませんでした。")
        break
    elif status == "dl_failure":
        print("処理に失敗しました。")
        break

    time.sleep(2)
```

### 主要なメソッドとプロパティ

- `core.new_request(url, is_video, video_codec, audio_codec, min_save_period, user)`: ダウンロードをリクエストします。
- `core.yt_search(query)`: YouTubeを検索し、結果のリストを返します。
- `core.video_dic`: 全てのリクエストを`uuid`をキーとした辞書で保持します。
- `core.queue_list`: ダウンロード待ちの`uuid`のリストです。
- `core.total_size()`: 保存されているファイルの合計サイズ(MB)を返します。

### 注意点

- **作業ディレクトリ**: `core.py`は、実行されたカレントディレクトリに`outputs/`と`log.txt`を生成します。ファイルのパスを扱う際はご注意ください。
- **環境変数**: `core.py`は動作のためにいくつかの環境変数を参照します。特にGoogle OAuthを利用する場合は設定が必須です。詳細は`app/core.py`の`settings`クラスを参照してください。

ここまでのドキュメントはGemini CLIを活用し作成しました。
## 3. 開発者より
このソフトウェアは、YouTubeMP3もどきの混雑化がきっかけで2024年初頭ごろに開発を始めました。当初はAPIのみで、iOSのショートカット機能でPOSTして使用していましたが、1年以上もの月日が流れ、幾度もの改善/改良の結果、この今の形にたどり着きました。 

（勝手にですが）参考にさせていただいたYouTubeMP3、そしてYouTubeMP3もどきの開発・運営されている方に感謝を申し上げます。