# chrome-web-mcp

[English README](README.md)

> **対応OS: Linux / macOS。** macOSではX11を使わず、通常のMac版Chromeを
> visibleまたは`--headless=new`で起動します。Windowsは未対応です。
> macOSのセットアップとプロジェクト内Chrome for Testingについては
> [docs/macos.md](docs/macos.md)を参照してください。

> [!CAUTION]
> 1つのプロファイルにつき、ブラウザは1つだけ使用してください。
>
> - 通常の利用ではレート制限にかかりません。1分間に15回以上の検索を
>   開始すると`pace_warning`が返ります。
> - `pace_warning`はブロックではありませんが、Googleは総量も見ているため、
>   サーバー自身が絞ります。`max_searches_per_hour`（既定60回）と、
>   CAPTCHA時の指数バックオフのクールダウンです。
> - GoogleのCAPTCHAが出ても検索は失敗しません。Googleを共有クールダウンに
>   入れ、同じクエリを`search_engines`の次のエンジン（既定ではDuckDuckGo、
>   Brave、Bing、任意で自前のSearXNG）が答えます。応答の`engine`が実際に
>   答えたエンジン、`attempts`が各エンジンの結果です。`show_browser: true`
>   （Xephyr）ならCAPTCHAの窓は表示されたまま残るので、自分で解けます。
>   現在の状態は`health_check`の`last_captcha_at`と`engine_cooldowns_s`で
>   確認できます。全エンジンがクールダウン中の場合は、数分待ってから再試行して
>   ください。
> - 別々のMCPプロセスは、永続プロファイルを共有します（同時にブラウザを
>   使えるのは1つ）。プロファイルが使用中の場合、もう一方のプロセスは自分専用の
>   使い捨てプロファイルへ自動的に切り替えます（`health_check`の
>   `profile_shared_fallback: true`）。同時に大量検索しないでください。

JavaScriptを実行できるChromeを使って、以下のMCPツールを提供するstdio
サーバーです。

- `google_search`: Google検索を実行し、構造化された結果を返します。Googleが
  CAPTCHAを出した場合は他のエンジンへ自動フォールバックします。
- `fetch_url`: 公開HTTP(S)ページを取得し、読みやすいテキストやMarkdownを返します。
- `health_check`: ブラウザ、表示モード、検索待ち時間、CAPTCHA、プロファイル、
  エンジンの状態を確認します。

## 特徴

- 実際のChrome/Chromiumを使ったJavaScript対応のGoogle検索とページ取得
- LinuxではXephyr/Xvfb、macOSではnative/headless Chromeを使用
- `trafilatura`と`html2text`による読みやすいMarkdown整形
- `hl`（Googleの表示言語）と`gl`（検索地域）の指定
- 公開アドレスだけに接続する検証プロキシ。localhostやプライベートIPを拒否
- CAPTCHA耐性: GoogleがCAPTCHAを出すと次のエンジン（DuckDuckGo、Brave、
  Bing、任意でSearXNG）が同じクエリに答えるため、1つのエンジンの失敗で
  ツール呼び出しが失敗しません
- 永続プロファイル: Cookieと同意状態が再起動後も残るため、Googleからは
  「初回訪問」ではなく「再訪したブラウザ」に見えます（`"profile": "ephemeral"`
  で従来の使い捨てプロファイルに戻せます）
- 一貫性を優先したフィンガープリント調整: UA、`navigator.platform`、クライアント
  ヒント、WebGLのベンダー文字列を互いに矛盾しないように揃えます（`spoof_platform`）
- Google検索の開始間隔、CAPTCHAクールダウン、時間あたりの検索予算を
  SQLiteでプロセス間共有
- 既定ではGoogleの検索ボックスに入力して検索（`/search?q=`への直接遷移ではない）。
  `"human_input": false`で直接遷移に戻せます
- Chrome、表示サーバー、プロキシの終了処理と孤児プロセスの回収
- Linux / macOS対応

## 必要環境

- Python 3.10以上
- Google Chrome、Google Chrome for Testing、またはChromium。macOSでは
  `CW_CHROME`の次にプロジェクト内`.local-chrome`のChrome for Testingを
  自動検出するため、通常のChromeを`/Applications`へ入れる必要はありません
- Linuxで窓を表示する場合: `Xephyr`とデスクトップの`DISPLAY`
- Linuxで窓を表示しない場合: `Xvfb`
- LinuxでCAPTCHAを対話的に解除する場合（任意）: `xpra`
- macOSではXvfb/Xephyr/DISPLAYは不要

## macOSでのインストール

通常のChromeを`/Applications`へインストールせず、プロジェクト内のChrome for
Testingだけで動かせます。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python scripts/install-chrome-for-testing.py
chrome-web-mcp
```

取得したブラウザはgit管理外の`.local-chrome/`へ保存されます。ブラウザの
ダウンロードはこのスクリプトを明示的に実行した場合だけ行われ、MCPサーバーの
起動時に勝手にダウンロードすることはありません。詳細は
[`docs/macos.md`](docs/macos.md)を参照してください。

## ヘッドレス環境

デスクトップのないサーバー、CI、X転送なしのSSHセッションでは、設定ファイルに
必ず次を指定してください。

```json
{
  "show_browser": false
}
```

設定ファイルの場所は通常`~/.config/chrome-web-mcp/config.json`です。
`CW_CONFIG`で別の場所を指定できます。変更後はMCPクライアントを再起動して
ください。

`show_browser: false`では、Linuxは隠しXvfb、macOSは`--headless=new`で
Chromeを起動します。内蔵デフォルトはデスクトップ利用向けの`true`です。
Docker版はLinuxコンテナ内でXvfbを使用します。

## Linuxでのインストール

Debian/Ubuntuでは、まずシステム依存パッケージをインストールします。

```bash
sudo apt update
sudo apt install -y chromium xvfb xserver-xephyr x11-utils python3-venv
```

ソースからインストールします。

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.'
```

### opencodeの設定

`~/.config/opencode/opencode.json`の`mcp`に追加します。

```json
{
  "mcp": {
    "chrome-web": {
      "type": "local",
      "command": [
        "/absolute/path/to/chrome-web-mcp/.venv/bin/chrome-web-mcp"
      ],
      "enabled": true,
      "timeout": 120000
    }
  }
}
```

`/absolute/path/to/chrome-web-mcp`は実際のチェックアウト先に置き換えて
ください。設定変更後はopencodeを再起動します。

ローカル版とDocker版を比較したい場合は、別名で追加できます。

```json
"chrome-web-docker": {
  "type": "local",
  "command": ["docker", "run", "--rm", "-i", "--security-opt", "seccomp=/absolute/path/to/chrome-web-mcp/docker/seccomp_profile.json", "chrome-web-mcp:latest"],
  "enabled": true,
  "timeout": 120000
}
```

## 設定ファイル

設定ファイルは次の2つをコピーして使います。

```bash
mkdir -p ~/.config/chrome-web-mcp
cp examples/config.json examples/config.md ~/.config/chrome-web-mcp/
```

JSONはコメントをサポートしていないため、説明は`config.md`にあります。
主な設定は次のとおりです。

```json
{
  "show_browser": true,
  "hl": "ja",
  "gl": "jp",
  "limit": 5,
  "char_limit": 15000,
  "format": "markdown",
  "min_delay": 1.0,
  "max_delay": 2.5,
  "profile": "persistent",
  "search_engines": ["google", "ddg", "brave", "bing"],
  "searxng_url": "",
  "human_input": true,
  "max_searches_per_hour": 60,
  "captcha_cooldown_s": 300,
  "captcha_cooldown_max_s": 1800,
  "spoof_platform": "native",
  "disable_gpu": true
}
```

- `show_browser`: `true`でブラウザを表示、`false`で非表示。macOSでは
  `true`がnative Chrome、`false`が`--headless=new`
- `hl`: Googleの表示言語。`ja`は日本語、`en`は英語
- `gl`: Googleの検索地域。`jp`は日本、`us`は米国
- `limit`: `google_search`の既定結果数（1から20）
- `char_limit`: `fetch_url`の既定最大文字数（100から200000）
- `format`: `markdown`、`text`、`links`のいずれか
- `min_delay` / `max_delay`: Google検索開始間隔の秒数。範囲内でランダム化
- `profile`: `persistent`（既定）はCookieなどを保持する永続プロファイル、
  `ephemeral`は従来の使い捨てプロファイル
- `search_engines`: 試すエンジンの順序。`searxng`は`searxng_url`が必要
- `searxng_url`: 自前のSearXNGのURL（例 `http://10.0.0.1:4000`、`format=json`許可が必要）
- `human_input`: `true`（既定）でGoogleの検索ボックスに入力して検索
- `max_searches_per_hour`: Googleのみの時間あたり上限（`0`で無効）
- `captcha_cooldown_s` / `captcha_cooldown_max_s`: CAPTCHA後のクールダウン初回値と
  指数バックオフの上限（秒）
- `spoof_platform`: `native`（既定）はLinuxのUAと`navigator.platform`を一致させる、
  `windows`はUA・platform・クライアントヒントをWindowsで揃える
- `disable_gpu`: `true`（既定）でソフトウェアレンダリングを使用

環境変数で上書きする場合は `CW_PROFILE_MODE`、`CW_SEARCH_ENGINES`、
`CW_SEARXNG_URL`、`CW_HUMAN_INPUT`、`CW_MAX_SEARCHES_PER_HOUR`、
`CW_CAPTCHA_COOLDOWN`、`CW_CAPTCHA_COOLDOWN_MAX` を使います（設定ファイルより
優先されます）。

日本語の検索結果にしたい場合は次を指定します。

```json
{
  "hl": "ja",
  "gl": "jp"
}
```

英語・米国向けにしたい場合は次です。

```json
{
  "hl": "en",
  "gl": "us"
}
```

`hl`と`gl`をツール呼び出しで省略すると、設定ファイルの値が使われます。
ツール呼び出し側で明示した値がある場合は、その呼び出しだけ明示値が優先されます。
設定ファイルを変更したらMCPクライアントを再起動してください。

## Docker

DockerイメージにはPython依存関係、Chromium、Xvfb、Xephyrなどが含まれます。

```bash
docker build -t chrome-web-mcp .
```

`--user`を指定しない場合は、Dockerfileで定義した非rootの`chrome`ユーザー
（UID/GID `1000:1000`）で起動し、Chromiumのサンドボックスを有効にします。
後述のXephyr表示の実行例では、この既定値をホストユーザーのUID/GIDで上書きします。

Dockerの既定制限では必要な名前空間を作れないため、
起動時に同梱の`docker/seccomp_profile.json`を指定してください。
MCP設定ではDocker CLIを実行するホスト上の絶対パスを使います。ホスト側でも
非特権ユーザーによる名前空間の作成が許可されている必要があります。
rootへの切り替えやサンドボックスの無効化で回避しないでください。

この設定は[Mobyの既定seccomp設定](https://github.com/moby/profiles/blob/61eaf32614c7c71b60bd8927d3e6a4ffc8ff1f31/seccomp/default.json)を基に、
[PlaywrightのDocker向け案内](https://playwright.dev/docs/docker#crawling-and-scraping)に従って
`clone`、`setns`、`unshare`の許可規則だけを追加しています。他の既定制限は維持し、
[Apache 2.0ライセンス](docker/LICENSE)に従って配布します。

通常のDocker版はコンテナ内のXvfbを使用するため、窓は表示されません。
opencodeでは`chrome-web-docker`として登録できます。

### Docker版でCAPTCHAが出た場合

Docker版で`captcha_required: true`が返った場合、CAPTCHAはDockerコンテナ内の
ブラウザに表示されています。ローカル版の窓を操作してもDocker版のCAPTCHAは
解除できません。

同じDocker版をXephyr表示で起動するには、ホストのDISPLAY、X11ソケット、
Xauthorityをコンテナへ渡します。

```bash
docker run -i --rm \
  --security-opt seccomp=docker/seccomp_profile.json \
  --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e DISPLAY=$DISPLAY \
  -e CW_DISPLAY_MODE=xephyr \
  -e XAUTHORITY=/tmp/.Xauthority \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v "$XAUTHORITY":/tmp/.Xauthority:ro \
  chrome-web-mcp
```

読み取り可能な`XAUTHORITY`を設定した、非rootのデスクトップユーザーとして実行します。
上の`--user "$(id -u):$(id -g)"`は、コマンドを実行するホストユーザーの
UID/GIDを取得して既定値を上書きします。認証ファイルの所有者とUIDを合わせることで、
権限0600の認証ファイルを読み取れます。
`HOME=/tmp`はUIDが1000以外でもChromeのホームに書き込めるようにする指定です。
プロファイル、ロック、検索間隔DBをマウントする場合も、そのUIDに書き込み権限が必要です。
Linuxのデスクトップ環境とXephyrが必要です。Wayland/Mutterでは、
`$XDG_RUNTIME_DIR`内の`.mutter-Xwaylandauth.*`が認証ファイルになる場合が
あります。その場合は、ホストの`XAUTHORITY`に該当ファイルを設定してから実行してください。

最も簡単なCAPTCHA対策は、デスクトップ環境では最初からローカル版の
`show_browser: true`を使うことです。

## 更新

### ローカルチェックアウト

```bash
git -C /absolute/path/to/chrome-web-mcp fetch origin
git -C /absolute/path/to/chrome-web-mcp reset --hard origin/main
```

履歴がforce-pushされることがあるため、`pull --ff-only`では更新できない場合が
あります。ローカル変更がある場合は、先に内容を確認してから`git stash`などで
退避してください。更新後はMCPクライアントを再起動します。

`uv run --directory ... chrome-web-mcp`形式なら、次回起動時に依存関係も同期される
ため、通常は別途再インストール不要です。専用venvを使っている場合は次を実行
します。

```bash
uv pip install --python ~/.local/share/chrome-web-mcp/venv/bin/python \
  -U -e /absolute/path/to/chrome-web-mcp
```

### Docker

ソースを更新したらイメージを再ビルドします。Dockerfileの編集は通常不要です。

```bash
docker build -t chrome-web-mcp /absolute/path/to/chrome-web-mcp
```

## ツール

### `google_search`

```json
{
  "query": "検索語",
  "limit": 5,
  "hl": "ja",
  "gl": "jp",
  "engine": "auto"
}
```

`query`は必須で最大512文字、`limit`は1から20です。結果にはタイトル、URL、
説明、順位のほか、次のフィールドが含まれます。

- `engine`: 実際に答えたエンジン（`google`、`ddg`、`brave`、`bing`、`searxng`）
- `attempts`: 試したエンジンごとの結果（`ok`と、エラーまたは結果件数）
- `captcha_avoided`: GoogleがCAPTCHAを出し、別のエンジンが答えた場合に`true`
- `waited_ms`: プロセス間の検索キュー待ち時間
- `pace_warning`: 1分間に15回以上で設定されます

`engine`の既定値は`auto`（設定順）です。エンジンを指定すると、そのエンジンを
最初に試し、残りはフォールバックとして残ります。検索結果を詳しく読む場合は、
次に`fetch_url`を使います。

GoogleがCAPTCHAを表示した場合、このサーバーはCAPTCHAを突破しません。
CAPTCHAを記録し、Googleを指数バックオフのクールダウンに入れ、同じクエリを
次のエンジンに投げます。`show_browser: true`（Xephyr）ならCAPTCHAの窓は開いた
まま残るので自分で解けます。すべてのエンジンが失敗した場合は
`{"success": false, "error": "...", "attempts": [...]}`を返し、CAPTCHAが原因の
場合は`captcha_required: true`も含まれます。

### `fetch_url`

```json
{
  "url": "https://example.com",
  "char_limit": 15000,
  "format": "markdown"
}
```

公開HTTP(S) URLだけを取得できます。localhost、プライベートIP、メタデータ用
ホスト、認証情報を含むURLは拒否されます。

- `markdown`: boilerplateを除いた読みやすいMarkdown。通常はこちら
- `text`: ページの全文。Markdown抽出で欠落がある場合に使用
- `links`: 本文に加えて、次に辿れるリンクも返す

`char_limit`は100から200000、URLは最大2048文字です。レスポンスには最終URL、
リダイレクト有無、総文字数、切り詰め有無、抽出方法が含まれます。

### `health_check`

引数はありません。platform、表示モード、Chrome binary/version、GPU backend、
Chrome/Xvfb/Xephyrの稼働状態、検索キューの待ち時間、直近の検索数、CAPTCHA時刻に
加えて、CAPTCHA耐性の状態を返します。

- `profile_mode`（`persistent`または`ephemeral`）、`profile_dir`、
  `profile_shared_fallback`（他プロセスが共有プロファイルを使用中で、自分専用の
  使い捨てプロファイルに切り替えた場合に`true`）
- `engines`（試行順）、`engine_cooldowns_s`（エンジンごとの残りクールダウン秒）、
  `searches_last_hour`、`max_searches_per_hour`、`budget_exhausted`
- `human_input`と`spoof_platform`（現在のフィンガープリント設定）

health_check自体はブラウザを起動しません。

## CAPTCHAについて

このサーバーは認証やCAPTCHAのパズルを自動的に解くものではありません。

GoogleがCAPTCHAを表示すると、サーバーはそれを記録してクールダウンに入り、
同じクエリを次のエンジンで実行します。したがって通常は検索が失敗せず、
応答は次のようになります。

```json
{
  "success": true,
  "data": {
    "engine": "ddg",
    "captcha_avoided": true,
    "attempts": [
      {"engine": "google", "ok": false, "error": "captcha", "cooldown_s": 300.0},
      {"engine": "ddg", "ok": true, "results": 5}
    ]
  }
}
```

`show_browser: true`（Xephyr）ならCAPTCHAの窓は表示されたまま残ります。窓で
CAPTCHAを解いてから同じ検索を再試行すると、次にGoogleが成功した時点で
クールダウンが解除されます。`show_browser: false`（Xvfb）の場合は、Googleが
クールダウン中は他のエンジンが答えます。

## セキュリティと範囲

- 任意のページコンテキストJavaScriptを実行するツールは提供しません
- ブラウザのHTTP(S)/WebSocket接続は公開アドレスに限定します
- URL内の認証情報や明らかな秘密情報パターンを拒否します
- サーバーごとにChromeと必要な表示backendを所有し、終了時に回収します
- Linuxは独立X11 display、macOSは独立temporary profileのnative/headless Chromeを使います
- これは一般的なリモートブラウザ操作APIではありません

## 謝辞

ブラウザを使ったWeb処理は、当初[antirez/ds4](https://github.com/antirez/ds4)を
土台として実装し、その後PythonおよびMCP向けに大幅に再設計しています。適用される
MITライセンスの通知は[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)を参照してください。

## 開発と検証

開発者向けのセットアップ、テスト、ビルド手順は
[`CONTRIBUTING.md`](CONTRIBUTING.md)を参照してください。

決定的なテストだけを実行する場合は次です。

```bash
pytest -q -m 'not live'
```

`live`マーク付きテストは実際にGoogleへ接続するため、ネットワーク状態や
CAPTCHAの影響で失敗することがあります。
