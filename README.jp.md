# chrome-web-mcp

[English README](README.md)

> **対応OS: Linuxのみ。** WindowsとmacOSはサポート対象外です。Dockerを
> 使用する場合も、ホストOSの対応条件は変わりません。

> [!CAUTION]
> 1つのMCPサーバープロセスにつき、ブラウザは1つだけ使用してください。
> 検索は連続して大量に実行せず、基本的に順番に実行します。
>
> - 通常の利用ではレート制限にかかりません。1分間に15回以上の検索を
>   開始すると`pace_warning`が返ります。
> - `pace_warning`はブロックではありませんが、短時間に大量検索を続けると
>   GoogleからCAPTCHAを要求されることがあります。CAPTCHAが出た場合は、
>   数分待ってから再試行してください。`show_browser: true`（Xephyr）なら
>   `chrome-web-mcp`の窓でCAPTCHAを解いてから同じ検索を再試行できます。
> - 別々のMCPプロセスは、それぞれ別のブラウザを起動します。複数のCLIや
>   MCPクライアントから同時に大量検索しないでください。

JavaScriptを実行できるChromeを使って、以下のMCPツールを提供するstdio
サーバーです。

- `google_search`: Google検索を実行し、構造化された結果を返します。
- `fetch_url`: 公開HTTP(S)ページを取得し、読みやすいテキストやMarkdownを返します。
- `health_check`: ブラウザ、表示モード、検索待ち時間、CAPTCHA状態を確認します。

## 特徴

- 実際のChrome/Chromiumを使ったJavaScript対応のGoogle検索とページ取得
- Xephyr（窓あり）またはXvfb（窓なし）による独立した仮想ディスプレイ
- `trafilatura`と`html2text`による読みやすいMarkdown整形
- `hl`（Googleの表示言語）と`gl`（検索地域）の指定
- 公開アドレスだけに接続する検証プロキシ。localhostやプライベートIPを拒否
- Google検索の開始間隔をSQLiteでプロセス間共有
- Chrome、表示サーバー、プロキシの終了処理と孤児プロセスの回収
- Linux専用

## 必要環境

- Python 3.10以上
- Google Chrome、Google Chrome for Testing、またはChromium
- 窓を表示する場合: Linuxの`Xephyr`とデスクトップの`DISPLAY`
- 窓を表示しない場合: Linuxの`Xvfb`
- CAPTCHAを対話的に解除する場合（任意）: `xpra`

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

`show_browser: false`ではChromeを隠しXvfb上で起動します。内蔵デフォルトは
デスクトップ利用向けの`true`なので、ヘッドレス環境ではこの設定を省略しないで
ください。Docker版はコンテナ内でXvfbを使用するため、通常はホストのDISPLAYを
設定する必要はありません。

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
  "command": ["docker", "run", "--rm", "-i", "chrome-web-mcp:latest"],
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
  "max_delay": 2.5
}
```

- `show_browser`: `true`で`chrome-web-mcp`の窓を表示、`false`で非表示
- `hl`: Googleの表示言語。`ja`は日本語、`en`は英語
- `gl`: Googleの検索地域。`jp`は日本、`us`は米国
- `limit`: `google_search`の既定結果数（1から20）
- `char_limit`: `fetch_url`の既定最大文字数（100から200000）
- `format`: `markdown`、`text`、`links`のいずれか
- `min_delay` / `max_delay`: Google検索開始間隔の秒数。範囲内でランダム化

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
  -e DISPLAY=$DISPLAY \
  -e CW_DISPLAY_MODE=xephyr \
  -e XAUTHORITY=$XAUTHORITY \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v "$XAUTHORITY":"$XAUTHORITY":ro \
  chrome-web-mcp
```

Linuxのデスクトップ環境とXephyrが必要です。Wayland/Mutterでは、
`$XDG_RUNTIME_DIR`内の`.mutter-Xwaylandauth.*`が認証ファイルになる場合が
あります。詳しい注意点は英語版READMEのDocker節にも記載しています。

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
  "gl": "jp"
}
```

`query`は必須で最大512文字、`limit`は1から20です。結果にはタイトル、URL、
説明、順位のほか、検索待ち時間`waited_ms`と`pace_warning`が含まれます。
検索結果を詳しく読む場合は、次に`fetch_url`を使います。

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

引数はありません。表示モード、Chrome/Xvfb/Xephyrの稼働状態、検索キューの待ち
時間、直近の検索数、CAPTCHA時刻を返します。health_check自体はブラウザを起動
しません。

## CAPTCHAについて

このサーバーは認証やCAPTCHAを自動的に突破するものではありません。

GoogleがCAPTCHAを表示すると、ツールは次のような結果を返します。

```json
{
  "success": false,
  "error": "...",
  "captcha_required": true
}
```

`show_browser: true`なら、デスクトップ上の`chrome-web-mcp`窓でCAPTCHAを解き、
同じ検索を再試行します。`false`なら数分待ってから再試行してください。

## セキュリティと範囲

- 任意のページコンテキストJavaScriptを実行するツールは提供しません
- ブラウザのHTTP(S)/WebSocket接続は公開アドレスに限定します
- URL内の認証情報や明らかな秘密情報パターンを拒否します
- サーバーごとにChromeと仮想ディスプレイを所有し、終了時に回収します
- ホストのWaylandセッションではなく、明示したX11仮想ディスプレイ上でChromeを動かします
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
