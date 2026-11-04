# 视译训练辅导 · shiyi-coach

> 来自阿雏苏苏子Liora

一个**本地运行**的视译训练网站：支持英译中 / 中译英练习、浏览器录音转写、逐句点评、术语预习、手动评分、练习记录、收藏、专业复盘，以及 Word / PDF 导出。练习数据默认保存在本地。





## 功能

- **双向练习**：英译中、中译英。
- **题组练习**：支持系统题组和自带材料；同组五道题强制去重，并排除近期练过的题。
- **录音与转写**：浏览器录音，接入语音转写服务。
- **逐句点评**：断句、问题证据、修改结果、拖点、原因说明、停顿节奏、用词句式与术语建议。
- **手动补充**：语气和节奏标签。
- **练习记录与收藏**：保存练习记录，收藏题目和点评，并可添加笔记。
- **专业复盘**：基于练习记录指出薄弱点并给出优化建议。
- **导出**：练习记录、收藏和复盘结果可导出为 Word / PDF。

## 技术栈

- 后端：Python / FastAPI
- 前端：原生 HTML / CSS / JavaScript
- AI 服务：DeepSeek 文本生成、DashScope 语音转写（完整 AI 功能需要配置密钥）

## 30 秒快速开始

环境要求：**Python 3.10+**。Node.js 仅用于前端语法检查，可选。

```powershell
git clone https://github.com/su0128-happy/shiyi-coach.git
cd shiyi-coach

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY 和 DASHSCOPE_API_KEY

.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8765
```

浏览器打开：

```text
http://127.0.0.1:8765
```

Windows 下也可以直接运行：

```text
start_server.bat
```

> 运行期间请保持启动服务的终端窗口打开。

如果 PowerShell 激活虚拟环境失败，可先执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

然后重新运行 `.\.venv\Scripts\Activate.ps1`。

## 配置密钥

复制示例配置：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```env
DEEPSEEK_API_KEY=你的文本生成服务密钥
DASHSCOPE_API_KEY=你的语音转写服务密钥
```

> `.env` 包含敏感信息，已被 `.gitignore` 排除，不要提交到 GitHub。

## 使用流程

1. 选择练习方向：英译中或中译英。
2. 使用系统题组，或上传 / 粘贴自己的材料。
3. 作答前查看本句术语预习。
4. 浏览器录音并转写，或手动输入作答。
5. 查看逐句点评：断句、问题证据、修改结果、拖点、停顿节奏、用词句式、术语建议。
6. 手动调整五维评分、语气和节奏标签。
7. 保存练习记录，收藏题目和点评，并添加自己的笔记。
8. 进行专业复盘，导出 Word / PDF。

## 数据与隐私

以下内容默认只保存在本地，不应上传：

```text
.env        # 真实 API Key
.venv/      # 本地 Python 虚拟环境
data/       # 练习记录、收藏、导出文件等
*.log       # 本地日志
```

- 练习数据默认存放在 `data/` 目录。
- `.env`、`data/`、`.venv/` 已被 `.gitignore` 排除。
- 如果曾公开或提交过真实密钥，请立即在服务平台撤销并重新生成密钥；必要时清理 Git 历史。
- 术语预习和本地规则反馈用于练习辅助，请结合原文、专业场景和个人目标判断。

## 项目结构

| 路径                 | 说明                   |
| ------------------ | -------------------- |
| `app.py`           | FastAPI 路由与练习 / 点评逻辑 |
| `static/`          | 前端页面、样式和交互           |
| `coach_prompt.md`  | 评分与反馈提示词             |
| `requirements.txt` | Python 依赖            |
| `.env.example`     | 环境变量示例               |
| `start_server.bat` | Windows 启动脚本         |
| `data/`            | 本地练习数据，不上传           |

<details>
<summary><strong>开发检查与发布前安全检查</strong></summary>

### 开发检查

```powershell
.\.venv\Scripts\python.exe -m py_compile app.py
node --check static/app.js
```

### 发布到 GitHub 前

确认 `.env`、`data/`、`.venv/` 没有被纳入版本控制：

```powershell
git status --short
git check-ignore -v .env data .venv
```

确认无误后再提交和推送。

</details>

<details>
<summary><strong>常见问题</strong></summary>

**1. PowerShell 无法激活虚拟环境？**  
执行 `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`，再运行激活命令。

**2. 端口 8765 被占用？**  
换一个端口启动：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8766
```

**3. 没有配置密钥能用吗？**  
完整 AI 功能需要 DeepSeek 和 DashScope 密钥。未配置时，可使用手动练习和本地规则反馈；系统生成、语音转写等 AI 功能以页面提示为准。

**4. 练习记录在哪里？**  
默认在项目根目录的 `data/` 中。该目录不会上传到 GitHub。

**5. 导出文件在哪里？**  
通过页面导出按钮生成，默认保存在本地 `data/` 目录。

</details>

## 说明

- 本项目默认将练习数据存放在本地 `data/` 目录。
- 本项目依赖第三方文本生成与语音转写服务，请自行确认服务条款、费用和密钥安全。
- AI 点评用于练习辅助，不替代人工判断。
