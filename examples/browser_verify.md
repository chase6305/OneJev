# Browser verification with Playwright and OneJev

[简体中文](#中文使用说明)

This example connects a browser action to a model judgment and a bounded retry loop:

```text
Playwright action → screenshot → OneJev probabilities → continue / retry / stop
```

The default task uses a bundled invoice page. It simulates a temporary payment failure,
then a successful retry. Everything happens locally; the page makes no payment requests.
OneJev receives the screenshot and the task description. The page's DOM state is recorded
separately and guards the demo's actions and final success verdict.

## Setup

Start your model server in its inference environment:

```bash
flide serve --model OmniJev/OneJev1.6-0.8B --port 8000
```

In another terminal, from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[browser]'
python -m playwright install chromium
python examples/browser_verify.py
```

If you already activated a project environment, use the last three commands. With Google
Chrome installed, `python examples/browser_verify.py --channel chrome` uses that browser
and does not require a Chromium download. Add `--headed` to display its window.

The model server may run in a separate environment or on another machine:

```bash
python examples/browser_verify.py --base-url http://127.0.0.1:8000 --channel chrome
```

`QEV_BASE_URL` and `TYPESAFE_BASE_URL` are also supported by the client. `--model`
accepts the server's model name or an alias; its default is `jev-latest`.

## Local scenarios

```bash
python examples/browser_verify.py --scenario success
python examples/browser_verify.py --scenario retry
python examples/browser_verify.py --scenario blocked
python examples/browser_verify.py --scenario exhausted --max-attempts 2
python examples/browser_verify.py --scenario pending --page-timeout 1 --max-observations 3
```

| Scenario | Page behavior | Expected workflow outcome when the model agrees |
| --- | --- | --- |
| `success` | First payment succeeds | `verified` |
| `retry` (default) | First payment fails temporarily; second succeeds | `verified` |
| `blocked` | Payment is declined | `review`, with no retry |
| `exhausted` | Every payment fails temporarily | `exhausted` at the attempt limit |
| `pending` | Payment remains in progress | `exhausted` at the observation limit; no second payment |

The model answers three questions in each demo observation:

- `completed` (`noul`): probability that payment is complete.
- `status` (`choice`): probabilities for unpaid, retry, processing, paid and blocked.
- `progress` (`score`): progress from no attempt (0) through incomplete (1) to complete (2).

Completion requires a `paid` model state, completion probability at least `--threshold`
(default 0.85), state confidence at least `--min-confidence` (default 0.6), and a `paid`
DOM state. Disagreement or low confidence produces `review`; it never authorizes an
additional payment. Only the demo's allowlisted payment button can be clicked.

`--max-attempts` includes the initial payment. `--max-observations` bounds model calls;
the final observation cannot authorize an action whose outcome cannot be observed.
`--timeout` bounds each model request; `--page-timeout` bounds each browser operation
or wait. A processing payment is observed again rather than resubmitted.

These thresholds are demonstration settings. A returned probability of 0.95 is a
model estimate, not a guarantee of 95% accuracy on your application. Evaluate actual
successful and failed examples before choosing production thresholds.

## Check an existing page

The `--url` mode loads one HTTP(S) page, captures the visible 1280×720 viewport and
evaluates your completion statement. It does not run the invoice workflow.

```bash
# Serve the bundled page in another terminal, from the repository root:
python -m http.server 8765 --bind 127.0.0.1 --directory examples
```

```bash
python examples/browser_verify.py \
  --url http://127.0.0.1:8765/browser_task.html \
  --goal 'Open ACME invoice INV-1042.' \
  --success 'The page shows invoice INV-1042 with status Unpaid.' \
  --wait-for '#status'
```

`--wait-for` optionally waits for a visible CSS selector. For a dynamic application,
use a selector identifying the settled result. This mode has no DOM success oracle:
`verified` means the model's completion probability met the configured threshold.
Authentication and custom actions can be added to `run_check` in your own integration.

## Artifacts and exit codes

Each invocation creates a distinct directory below `runs/browser/` (or `--output`):

```text
20261103T030313Z-6aea5fd9/
  00.png
  01.png
  02.png
  observations.jsonl
  report.json
```

Screenshots show exactly what was sent to the model. The JSON records retain raw
answers, page states, chosen actions, usage, request IDs, client request times, and
server metadata including Flide's `onejev.timing`. `report.json` includes the final
result, reason, task description and run settings. Invalid model answers are retained
with an error decision; non-finite values are represented as text. Base64 image payloads are omitted from JSON; screenshots are
stored as separate files.
When a page operation or model request fails after the page has opened, the script
also tries to save `error.png` before closing the browser.

| Exit code | Meaning |
| --- | --- |
| 0 | `verified` |
| 1 | `review` or `exhausted` |
| 2 | Invalid arguments, unavailable server/browser, or invalid response |
| 130 | Interrupted with Ctrl+C |

Run the deterministic regression checks without a GPU or browser:

```bash
python -m unittest discover -s tests -v
```

For deterministic checks of the actual browser, HTTP image payloads, process exit
codes and saved reports, enable the optional integration tests. They use a local
HTTP server with scripted model replies and need no GPU:

```bash
QEV_BROWSER_TESTS=1 python -m unittest discover -s tests -v
# Use an installed Chrome instead of Playwright Chromium:
QEV_BROWSER_TESTS=1 QEV_BROWSER_CHANNEL=chrome python -m unittest discover -s tests -v
```

## 中文使用说明

这个示例把 **Playwright 操作网页 → 截图 → OneJev 判断 → 继续、重试或停止**
连接成一个可以直接运行的流程。默认页面是本地模拟的 ACME 发票，不涉及真实付款。
模型只读取截图和任务描述；脚本另外记录页面的真实状态，用于核对模型的判断。

先保持模型服务运行，再在仓库根目录执行：

```bash
source .venv/bin/activate
pip install -e '.[browser]'
python -m playwright install chromium
python examples/browser_verify.py
```

如果系统已经安装 Google Chrome，可以跳过浏览器下载，运行：

```bash
python examples/browser_verify.py --channel chrome
```

默认 `retry` 场景会经历“未付款 → 临时失败 → 重试 → 付款成功”。其他场景：

- `--scenario success`：第一次操作成功。
- `--scenario blocked`：明确失败，停止并要求人工检查。
- `--scenario exhausted --max-attempts 2`：持续失败，达到总尝试次数上限后停止。
- `--scenario pending --page-timeout 1 --max-observations 3`：一直处理中，达到观察次数上限后停止，不重复提交。

每一步显示完成概率和状态置信度。默认完成阈值为 0.85，状态置信度阈值为 0.6。
只有模型判断与页面真实状态一致时，演示才会报告成功。阈值需要用实际业务样本验证，
模型给出的概率不能直接当作业务准确率。

用 `--url` 检查已有网页时，还需要提供 `--success '期望看到的完成状态'`。
可用 `--goal` 补充任务背景，`--wait-for` 等待页面元素出现。这个模式只进行一次截图判断，
不会执行本地发票操作，也没有演示页面的真实状态校验。

截图、原始模型结果、每一步动作和耗时保存在 `runs/browser/` 下的独立目录中。
查看 `report.json` 可以了解最终结果及原因。退出码 0 表示判断通过，1 表示未通过或达到上限，
2 表示运行出错。`runs/` 已由仓库忽略，不会进入提交。
