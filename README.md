# cli-deck

[English](README.en.md) · [繁體中文](README.md) · [日本語](README.ja.md)

![cli-deck mascot](docs/mascot.png)

**終端機視窗關掉了，裡面跑的程式也不會跟著死——之後還能接回原本的畫面，就像你根本沒離開過。**

可以把它想像成保母（daemon）：你人不在，它幫你顧程式。
全螢幕儀表板則像一支遙控器，想放就放，拿起來繼續用。

| 特色 | 怎麼做到的 | 例子 |
|---|---|---|
| **程式活得比視窗久** | 程式是由背景的 daemon 持有，不是掛在你的終端機上。儀表板關掉，什麼都不會死。 | `cli-deck ssh prod`，然後直接把終端機關了 |
| **隨時回到現場** | 重新打開儀表板，選那列按 `t`，全螢幕接管：你打的鍵直接進程式，程式的畫面直接出來。 | `cli-deck --resume` |
| **shell 環境照用不誤** | 指令走 `bash -ic` 執行，你的 alias、function、PATH 全都還在。 | `cli-deck vim notes.md` |
| **離開期間發生了什麼，接回來先回放一遍** | 最近約 200 KB 的畫面輸出會存著，重新接上時直接重播。 | 按 `t` |
| **用看的就好，不會按到** | `enter` 開唯讀的即時輸出加歷史捲動，按任何鍵都不會送進程式。 | 按 `enter` |
| **掛了自動救回來（保護模式）** | 按 `p` 標記後，程式自己結束時 daemon 會自動把它重啟。 | 按 `p`，或 `cli-deck -p <cmd>` |
| **日誌留著，但不會撐爆磁碟** | 每個程式有一份原始日誌，成長有上限，到頂自動截半。 | `cli-deck -d <cmd>` 會列出日誌路徑 |

## 安裝

### 從原始碼

裝好 uv，在 repo 根目錄跑：

```sh
make install   # uv tool install .  -> PATH 上會有 `cli-deck`，同時安裝 shell wrapper
```

裝完開一個新 shell（或 `source ~/.bashrc.d/cli-deck`）讓 wrapper 函式生效。
要移除跑 `make uninstall`；開發環境用 `make deps`。
[Wrapper 細節](docs/reference.md#shell-wrapper)（英文）。

### AppImage（Linux）

到 [Releases](https://github.com/scott1028/cli-deck/releases) 下載 AppImage：
Intel/AMD 拿 `x86_64`，ARM 拿 `aarch64`。檔裡已內含 Python，不用裝 uv。
下載後改名成 `cli-deck.AppImage`，安裝：

```sh
install -Dm755 cli-deck.AppImage ~/.local/bin/cli-deck
```

環境沒有 FUSE 的話，跑的時候前面加 `APPIMAGE_EXTRACT_AND_RUN=1`。

### macOS（tarball）

到 [Releases](https://github.com/scott1028/cli-deck/releases) 下載 tarball：
只有 Apple Silicon 版（`macos-aarch64`），一樣內含 Python。
解壓到喜歡的位置，把啟動器塞進 PATH：

```sh
tar xzf cli-deck-macos-aarch64.tar.gz -C ~/.local/share/cli-deck
ln -sf ~/.local/share/cli-deck/bin/cli-deck ~/.local/bin/cli-deck
```

第一次跑被 macOS 擋下來，把下載標記清掉就好：
`xattr -dr com.apple.quarantine ~/.local/share/cli-deck`。

### Windows（WSL）

沒有原生 Windows 版。在 WSL 裡照 Linux 的方式裝就好：從原始碼或 AppImage。

## 一行就能開始

```sh
cli-deck vim notes.md    # 把 vim 交給 deck 跑，同時打開儀表板
```

終端機隨時關，vim 照樣跑。之後要回來：

```sh
cli-deck --resume        # 接回正在跑的 deck（縮寫：cli-deck -r）
cli-deck -l              # 列出還活著的 deck（名稱、socket、pid、跑了多久）和裡面的程式
```

選 vim 那列按 `t` 就接管畫面，`Ctrl-]` 回儀表板。整個過程程式沒停過一秒。

## 常用指令

| 指令 | 在做什麼 |
|---|---|
| `cli-deck` | 打開儀表板（需要時順手把 daemon 起起來） |
| `cli-deck <command>` | 把指令交給 deck 跑，然後打開儀表板 |
| `cli-deck -r` / `--resume` | 接回正在跑的 deck |
| `cli-deck -l` | 列出活著的 deck 和每個 deck 裡的程式，方便挑一個接回 |
| `cli-deck -d <cmd>` | 只註冊不打開儀表板，印出接回方法（不帶指令就只把 daemon 起起來） |
| `cli-deck -p <cmd>` | 開保護再跑，跟按 `p` 一樣（預設連續快速重啟最多 5 次、每次等 5 秒）；要改就用 `=` 黏著：`-p=-1,2` = 不限次數、等 2 秒 |
| `cli-deck --stop <id>` | 停止一列 |
| `cli-deck --restart <id>` | 重啟一列：同一組指令，id 換新的 |
| `cli-deck --rm <id>` | 移除已經結束的列，日誌留著 |
| `cli-deck --up <id>` | 關掉一列的保護（長寫法 `--unprotect`） |
| `cli-deck -- -L 80:localhost:80` | `--` 之後全部算你的指令 |

`-l` 每個 deck 印一行，底下縮排列出它的程式，欄位跟儀表板一樣；
`protect` 欄有值就是受保護的列。deck 記錄的 pid 還在，就算還活著；
死掉的項目會自動清掉。deck 的名字來自 `CLI_DECK_NAME`（預設 `default`）。

`--stop`、`--restart`、`--rm`、`--up` 只作用在 `CLI_DECK_NAME` 指定的 deck。
受保護的列要先 `--up`，才能 `--stop` 或 `--rm`，見
[從 shell 操作列](docs/reference.md#row-actions-from-the-shell)（英文）。

## 儀表板按鍵

| 按鍵 | 動作 |
|---|---|
| `a` | 新增程式（留空 = 純 bash，否則就是你的指令） |
| `enter` | 唯讀即時輸出＋歷史捲動（按鍵不會送出去） |
| `t` | 全螢幕接管選取的程式 |
| `Ctrl-]` | 離開接管，回儀表板 |
| `x` | 停止或移除（執行中的列會先問；預設 No） |
| `r` | 重啟：同一組指令、cwd、環境，列 id 會換新的 |
| `p` | 保護：程式自己結束時自動重啟 |
| `d` | 關掉儀表板；所有東西繼續跑 |
| `q` | 停掉 daemon（會先問——執行中的程式會被終止） |
| `b` | 返回列表 |
| `esc` | 直接送給程式，vim 之類照常可用 |

確認框預設停在 **No**，按 Enter 等於取消，不怕手滑。
[逐鍵行為](docs/reference.md#dashboard-key-details)（英文）。

## 原理（極簡版）

兩塊：一個獨立跑的 daemon 在 pseudo-terminal 裡持有程式，
加上一個用完可丟的 Textual TUI，兩者透過 Unix socket 溝通。
daemon 有自己的 session、不理 SIGHUP，所以關 TUI、甚至關掉整個終端機都殺不到程式。
接管時 TUI 暫停，把你的終端機和程式的 pty 兩邊直接對接原始位元組，
vim、ssh、htop 這種全螢幕程式才會渲染得跟原本一模一樣。

## 開發

```sh
make deps    # uv sync
make test    # unittest 測試套件
```

把 `pyproject.toml` 的 `version` 改高推到 `master`，CI 會跑測試、
蓋出 Linux AppImage 和 macOS tarball，然後發布一個標籤為
`v<version>-<commit 前 8 碼>` 的 release。

## 更多細節

- [完整參考](docs/reference.md)（英文）：架構、協定、保護模式、teardown、
  執行期檔案、環境變數、repo 結構。
