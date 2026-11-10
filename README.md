# 顺译 · 多源翻译 · OmniTranslate

面向 **Win顺 (WinShun)** 的多源流式翻译、双语词典与编程命名常驻插件（`anghost.translate`）。

## 用途与特性

1. **开箱即用（双免 Key 服务默认开启）**：
   - **有道词典（免 Key）**：输入单个单词或 2~3 词短语时自动显示英/美音标、核心中文释义前置、词形变化与双语例句。
   - **微软翻译（默认免 Key）**：未填写微软订阅密钥时使用免 Key 接口，填写后自动切换为 Azure 认知服务官方接口。
2. **多服务并发与 SSE 流式对比**：
   - 同时支持 **智谱 GLM**、**自定义 AI 大模型**（OpenAI 兼容 / DeepSeek / 通义千问 / 豆包方舟 / Kimi / 硅基流动 / OpenRouter / Anthropic / Azure / 本地 Ollama）、**DeepL / DeepLX**、**百度翻译**、**有道智云 v3**、**火山翻译**、**腾讯翻译 (TMT)** 与 **Google 翻译**。
   - 通过 `query/partial` 实时流式推送大模型输出，自动剥离 `<think>…</think>` 思考过程并显示「思考中…」状态，自动适配各厂商「关闭思考」参数分档。
3. **智能语言识别、段落接行与程序员工作流**：
   - 采用 CJK 占比先验算法（避免“中文句子夹杂英文单词”被误判为英语而中译中）。
   - 支持 `:en`、`:zh`、`:ja`、`:ko`、`:fr`、`:de`、`:es`、`:ru` 等前缀强制指定目标语言。
   - 支持「段落接行」，自动将 PDF 复制出的同段落断行拼接为完整句子。
   - **代码标识符自动拆词与变量命名生成**：输入 `camelCase` / `snake_case` / `CONSTANT_CASE` 标识符自动拆解翻译；中文短语译为简短英文时自动生成 `camelCase`、`PascalCase`、`snake_case`、`CONSTANT_CASE` 编程变量命名行。
4. **全局划词唤起与静默替换**：
   - 利用宿主 `hostHotkeys` 能力注册全局快捷键：
     - **划词翻译（默认 `Alt+D`）**：在任意软件选中文字后按下，自动取词（备份剪贴板 → 模拟 `Ctrl+C` → 还原剪贴板）并唤起 Win顺 搜索面板翻译。
     - **静默划词替换（默认 `Alt+Shift+D`）**：在输入框选中文字后按下，不弹窗口，后台请求第一个启用的翻译服务并原地替换为译文。
5. **长文字展开 (`#117`) 与详情对比面板 (`#202`)**：
   - 选中译文行时，列表下方自动展开多行全文区；开启详情预览面板（`Alt+P`）可同屏对比所有服务的完整译文。
6. **翻译历史、收藏与 Anki 导出**：
   - 查询阶段无任何副作用；仅当用户按 `Enter` 复制或 `Ctrl+Enter` 收藏时才写入历史。
   - 支持一键导出 **Anki 记忆卡 (TSV)** 与 **带 BOM 的 CSV 表格**。

---

## 命令关键字与示例

| 命令 | 关键字 | 示例 | 键盘动作 |
| :--- | :--- | :--- | :--- |
| **多源翻译** | `ot`、`sy`、`顺译`、`翻译` | `ot 请帮我 review 一下这个 PR`<br>`ot :ja 你好，世界`<br>`ot ephemeral`<br>`ot 用户账户余额` | `Enter` 复制译文并记入历史<br>`Ctrl+Enter` 收藏并复制<br>`Shift+Enter` SAPI 语音朗读 |
| **双语词典** | `od`、`cd`、`查词` | `od ephemeral`<br>`od look up` | `Enter` 复制当前词条/释义<br>`Ctrl+Enter` 收藏词条<br>`Shift+Enter` 朗读单词 |
| **翻译历史** | `trh`、`翻译历史` | `trh deadlock`（搜索历史）<br>`trh fav`（只看收藏）<br>`trh export`（导出 Anki TSV / CSV）<br>`trh clear`（清空非收藏历史） | `Enter` 复制译文<br>`Ctrl+Enter` 重新翻译原文<br>`Shift+Enter` 切换收藏状态 |

---

## 运行依赖

- **Python 3.10+**（仅使用 Python 标准库：`json`、`urllib`、`hashlib`、`hmac`、`threading`、`ctypes`，**零第三方 pip 依赖**，无需打包虚拟环境）。
- **Windows 10 / 11**（最低宿主版本 `minWinShun: 0.18.0`）。

---

## 权限说明

本插件以普通用户权限运行（不申请 `elevated`，也不申请 `processes` 子进程权限），在 `plugin.json` 中仅声明以下实际需要的权限：

| 权限 | 用途说明 |
| :--- | :--- |
| `network` | 向用户已启用的翻译与词典接口发送 HTTPS 请求（仅当用户输入查询或按下翻译热键时才向已开启的服务联网；日志仅记录服务 ID 与耗时，绝不记录原文、译文、URL 和密钥）。 |
| `ui` | 通过宿主 `register_hotkeys` 工具注册「划词翻译」与「划词替换」全局快捷键，并在划词后通过 `show_launcher` 唤起 Win顺 搜索框。 |
| `clipboard:read` | 在用户按下全局划词快捷键时读取复制的选区文本（并在取词前备份剪贴板文本），以及在输入空 `ot ` 时展示剪贴板快捷翻译入口。 |
| `clipboard:write` | 在划词取词完成后还原用户原有的剪贴板内容，以及在「静默划词替换」动作中写入译文。普通查询 (`query`) 阶段绝不修改剪贴板。 |

---

## 网络连接说明（连哪里、发什么）

插件仅向用户在设置中**显式开启**的翻译/词典服务发起请求，发送内容仅为**当前查询的待翻译文本、源语言与目标语言代码**，不上传任何其他本地数据或遥测信息：

| 服务开关 | 连接的目标地址 | 发送的内容 |
| :--- | :--- | :--- |
| **有道词典** (`enableYoudaoDict`) | `https://dict.youdao.com/jsonapi` | 单词或短语文本 `q`（仅当输入为单个词或 2~3 词短语时触发） |
| **微软翻译** (`enableMicrosoft`) | 未填密钥：`https://edge.microsoft.com/translate/translatetext`<br>填写密钥：`https://api.cognitive.microsofttranslator.com/translate` | 待翻译文本、源语言与目标语言代码（及可选订阅密钥请求头） |
| **智谱 GLM** (`enableZhipu`) | `https://open.bigmodel.cn/api/paas/v4/chat/completions` | 待翻译文本、翻译/词典系统提示词、模型名与 Bearer API Key |
| **AI 大模型** (`enableAi`) | 用户在 `aiBaseUrl` 填写的接口地址（如 `https://api.deepseek.com` 等） | 待翻译文本、翻译/词典系统提示词、模型名与 API Key |
| **DeepL** (`enableDeepl`) | `https://api-free.deepl.com/v2/translate` 或 `https://api.deepl.com/v2/translate`（或用户配置的 `deeplxUrl`） | 待翻译文本、源语言、目标语言与 DeepL Auth Key |
| **百度翻译** (`enableBaidu`) | `https://fanyi-api.baidu.com/api/trans/vip/translate` | 待翻译文本、语言代码、`appid`、随机盐与 MD5 签名 |
| **有道智云 v3** (`enableYoudao`) | `https://openapi.youdao.com/api` | 待翻译文本、语言代码、`appKey`、时间戳与 SHA256 签名 |
| **火山翻译** (`enableVolcengine`) | `https://open.volcengineapi.com/?Action=TranslateText&Version=2020-06-01` | 待翻译文本、目标语言与 HMAC-SHA256 V4 签名头 |
| **腾讯翻译** (`enableTencent`) | `https://tmt.tencentcloudapi.com/` | 待翻译文本、源/目标语言与 TC3-HMAC-SHA256 签名头 |
| **Google 翻译** (`enableGoogle`) | `https://translation.googleapis.com/language/translate/v2` | 待翻译文本、源/目标语言与 Google Cloud API Key |

---

## 数据位置与密钥存储

- **密钥存储**：所有服务的密钥（`zhipuKey`、`aiKey`、`deeplKey`、`baiduSecret`、`youdaoAppSecret`、`volcSecretKey`、`tencentSecretKey`、`googleKey`、`microsoftKey`）均声明为 `password` 类型，由 Win顺 宿主加密保存在 **Windows 凭据管理器** 中，不写入源码、普通配置文件或日志。
- **本地数据目录 (`WS_DATA_DIR`)**：
  - 翻译历史与收藏保存在 `<WS_DATA_DIR>\history.json`（即 `%LOCALAPPDATA%\WinShun\plugins\anghost.translate.data\history.json`）。
  - 导出的 Anki TSV 与 CSV 文件保存在 `<WS_DATA_DIR>\exports\` 下。

---

## 失败处理

- **未配置密钥 / 配置错误**：对应服务行提示具体缺少的配置项，按 `Enter` 直接跳转 `winshun:settings` 打开本插件设置页。
- **网络超时 / 服务异常**：单次查询总预算控制在 `8.5 秒` 内（低于宿主 `10 秒` 上限）；超时、断网或上游返回 429/5xx 时显示明确中文错误提示，绝不返回伪造译文。
- **输入取消**：收到 `cancel(queryId)` 或新查询时立即中止未完成的流式请求，不把半截译文写入缓存或历史。

---

## 许可证

MIT License
