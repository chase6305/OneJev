# OneJev Studio

一个持续运行的本地交互网站，整合浏览器任务、文本判断、图片判断和运行记录。
案例和模型状态都在网页里管理，日常只需要一个启动入口。

## 启动

在仓库根目录、已经安装项目的 Python 环境中运行：

```bash
pip install -e '.[browser]'
qev studio
```

打开 **http://127.0.0.1:7860**。也可以使用 `python -m qev.studio`。
系统安装了 Google Chrome 时会自动使用它；其他环境先运行
`python -m playwright install chromium`。

网站会连接已运行的模型服务；如果本机服务尚未运行，会尝试自动启动 Flide。
页面在模型加载时仍然可以打开，右上角状态会自动更新。

如果网站环境和 GPU 推理环境不同，第一次指定已经安装 Flide 的 Python：

```bash
qev studio --inference-python /path/to/inference/.venv/bin/python
```

这个设置保存在当前工作目录的 `runs/studio/runtime.json` 中，之后仍然只需运行
`qev studio`。默认模型是 `OmniJev/OneJev1.6-0.8B`，默认推理端口为 8000，
网站端口为 7860。模型、端口和推理环境的首次准备仍需按项目快速开始完成。

连接另一台机器或已有服务时，可以使用：

```bash
qev studio --backend-url http://model-host:8000 --no-autostart
```

## 网页里的四个入口

**浏览器任务**：从本地模拟发票开始，提供首次成功、临时失败后重试、明确拒绝、
重试耗尽和一直处理中五种场景。默认单步控制，每次点击后展示截图、完成概率、
当前状态和下一步动作。下一次点击才执行建议的动作，再观察结果。

点击“自动演示”可以连续运行，默认每步停留 3 秒，可调为 0.5–10 秒。
“暂停”会停止后续步骤；正在进行的模型判断会先完成。成功或失败后，画面会保留，
不会自动关闭。点击观察轨迹里的缩略图可以回看任意一步，点击主截图可以放大。
“重新开始”或更换场景会创建新案例。

**文本判断**：提供客服分流与任务完成两个预设。可修改示例文本，查看部门选择、
紧急程度、不满程度或自定义命题的概率。

**图片理解**：上传 PNG、JPG 或 WebP，填写想确认的命题。也可以点击
“先用案例截图试一下”，判断发票是否已付款。图片上限 10 MB、1600 万像素。

**运行记录**：浏览最近 30 次浏览器案例，逐步回放截图与原始模型输出。
记录保存在本地，重启网站后仍可回看；重启前正在进行的案例需要重新开始。

## 结果与生命周期

浏览器案例复用命令行示例的判断策略：必须同时满足完成概率阈值、状态概率阈值、
模型状态和页面真实状态，才报告完成。临时失败最多尝试两次（含第一次），
一直处理中最多观察六次。明确拒绝、判断冲突或低置信度会停止。

这些是演示阈值，不能直接当作业务准确率。文本和图片判断返回模型概率，
其中没有发票演示的页面真实状态校验。

网站只管理由自己启动的模型进程；复用外部服务时不会在退出网站时停止它。
关闭启动网站的终端或按 Ctrl+C 会结束网站；想持续运行时，可使用你常用的
进程管理工具管理同一条 `qev studio` 命令。

截图和报告位于 `runs/studio/<案例 ID>/`。`web.log`（如启动器配置了日志重定向）
与 `backend.log` 用于排查问题，均不进入 Git 提交。

## 验证

```bash
# 单元检查
python -m unittest discover -s tests -v

# 包括真实 Chrome、本地 HTTP 与可复现模型响应的集成检查，无需 GPU
QEV_BROWSER_TESTS=1 QEV_BROWSER_CHANNEL=chrome python -m unittest discover -s tests -v
```

## English

Run `qev studio` (or `python -m qev.studio`) and open `http://127.0.0.1:7860`.
The local website combines browser decisions, text checks, image checks and persistent
browser-run history. Browser demonstrations support manual steps, automatic playback,
pause/resume, adjustable pacing and screenshot replay; final results stay visible.

Studio reuses an available System One server and can start a local Flide server itself.
For a separate GPU environment, set `--inference-python /path/to/python` once; the path is
saved locally for subsequent launches. Use `--backend-url` and `--no-autostart` for an
externally managed server. Studio only stops inference processes it started itself.

Install the `browser` optional dependencies, and use installed Chrome or install Chromium
with `python -m playwright install chromium`. Model weights and GPU inference dependencies
must be prepared in the inference environment. The website uses the existing shared
decision policy and saves browser reports and screenshots under `runs/studio/`.
