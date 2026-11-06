# Viser 机器人动作展示

机器人工作台已集成到 OneJev Studio。日常仍然只启动 `qev studio`，然后打开
**http://127.0.0.1:7860/#robot**，或在网站左侧选择“机器人动作”。

## 首次安装

在网站的 Python 环境中安装可选依赖：

```bash
pip install -e '.[browser,robot]'
qev studio
```

已安装的 Chrome 会自动识别。如果服务器没有 Chrome，可先运行
`python -m playwright install chromium` 安装内置相机使用的浏览器。

Viser 在首次打开机器人页时由 Studio 自动启动和关闭。三维页面与 WebSocket
都通过网站同一个地址提供；SSH 转发时只需要转发网站端口，不需要单独开放 Viser 端口。

## 案例：红色方块放入蓝色托盘

场景使用程序生成的简化六轴机械臂和平行夹爪，不需要下载额外的机器人模型。
可用鼠标旋转、缩放和平移三维视图。

上方“选择一个案例”提供四个预设：正常抓放、抓空后重试、掉落后恢复和目标移动。
点击“载入案例”会创建一条新记录，然后可逐步执行或开始自动演示。每次预设故障都会
作为独立步骤出现并停留，不必手动把握注入时机；故障与恢复都进入轨迹记录。
“重置场景”会重新载入当前案例。手动故障按钮仍可使用；手动注入同一预设故障后，
脚本不会再重复注入。

## 一键全自动

选择案例后，直接点击“**一键全自动**”即可从初始场景运行到动作结束，不需要先载入
或逐步确认。它会自动执行动作、注入预设故障并恢复；开启“动作后自动判断”时，每步
还会获取固定相机画面、请求模型并保存结果。已有演示会保留为历史记录。

这个入口会关闭“需要复核时暂停”。模型不确定或判断与场景验收不一致时，只记录原始
结果，继续播放；结束后显示检查次数和需要复核的次数，不会把这些判断改成成功。
如果不希望自动继续，可先暂停并勾选“需要复核时暂停”，再用工具栏的“自动演示”从当前
动作继续。执行错误或持续的视觉请求失败仍会暂停；暂停按钮始终可用。

Studio 会在第一次视觉检查时启动并管理内置相机渲染。它不依赖交互页面的自由视角，
可以切到其他页面或关闭交互浏览器，让服务端继续连播和模型检查。
相机下方会显示“内置相机已就绪，可关闭交互页面”。

如果内置浏览器无法启动，会尝试使用已连接的页面相机，并明确显示
“使用页面相机，请保持三维页面打开”；此时应保留页面。没有可用相机时会暂停并保留
现场，不会伪造图像。内置相机进程断开后，下次检查会重建连接，关闭 Studio 时会一并清理。

全自动指演示流程自动运行，动作仍由预设技能与逆运动学执行，当前版本不包含模型
自主规划或真机控制。

三维视图中的原生设置面板默认收起，可点击“视图设置”显示或隐藏。“复位视角”只恢复
本标签页的相机视角，“重连视图”只重新连接三维页面，两者都不会重置机械臂或推进动作。
“放大三维视图”进入全屏；按 Escape 或点击退出全屏返回。手机端同样可以使用这些控制。

“执行下一动作”会依次运行靠近、闭合夹爪、抬起、放入托盘和收回机械臂。
逆运动学计算目标关节位置，动作以平滑关节插值展示。开启“动作后自动判断”时，
每个技能结束后会请求固定相机渲染，并调用已有 OneJev 服务。

“让模型看一眼”只进行视觉判断，不执行任何动作。“自动演示”默认在每个技能之间
停留 3 秒，可用滑块调整为 0.5–15 秒，动作速度可选 0.25–2 倍。暂停会在当前技能
结束后生效。开启“需要复核时暂停”后，判断不确定或与场景验收冲突时会暂停。
持续的模型请求失败或执行失败会暂停。
仍可手动检查画面和执行技能。若只想播放动作，可关闭“动作后自动判断”。

自动演示由服务端统一推进。刷新页面、切换网站标签或关闭浏览器标签不会另开一轮演示，
重新进入会显示当前进度；要停止后续动作，请点击“暂停演示”。同一 Studio 的多个页面
共享播放、调速、故障和回放状态；过期页面的动作请求会被拒绝并刷新状态。
速度、停留时间和自动判断选项会保存在本地，重启网站后继续使用，但不会自动恢复执行。

网站连接中断时，会显示提示并暂时禁用场景操作，恢复连接后重新同步同一条记录。
服务端的自动演示可能仍在继续，不会因为浏览器暂时断线而重复开始。
如需暂停，恢复连接后点击“暂停演示”。内置相机就绪时，关闭交互页面不影响相机检查；
若正在使用页面相机回退模式，则仍需保留页面。

## 连播与结果汇总

“播放范围”可选择当前案例，或依次播放正常抓放、抓空重试、掉落恢复、目标移动。
轮数可选 1–20；例如选择四个案例和两轮，会创建八次独立演示。点击“一键全自动”
开始，案例之间默认停留 5 秒，页面显示切换倒计时。

“暂停演示”会保留当前案例和连播进度。若在案例间暂停，会停留在已完成的场景，
点击工具栏“自动演示”后再切换到下一例。“结束连播”会取消剩余案例，当前步骤
完成后停留，已有报告继续保留。点击“一键全自动”则创建一条新的任务。

“连播进度与记录”汇总每例的完成状态、视觉检查数、需要复核的次数和请求异常数。
暂停或结束后，可点击“回看”查看某一例的三维记录，也可导出汇总 JSON。
下拉框可选择最近 100 条历史连播记录。报告保存在
`runs/studio/robot/playlists/<连播 ID>/report.json`，各案例的独立报告通过 `playlist_id`
关联。网站重启后可回看历史，但不会自动恢复旧任务的执行。未完成的旧任务会标为
“已中断”，同时保留重启前的状态。完成或结束的汇总固定在该次执行的记录点，
后续手动操作不会改写汇总，回看也会显示结束时的姿态。

## 视觉请求失败后的恢复

自动演示遇到临时网络、相机超时或服务繁忙时，会对当前场景重试一次视觉检查，
不会重新执行刚才的动作。失败的请求、当时已捕获的画面和重试后的结果都保留，
可在“视觉请求异常记录”或报告中查看。请求异常数包含已成功恢复的重试。

两次请求仍失败时，演示暂停并保留姿态。重新点击“自动演示”会先重试当前画面的
判断，通过后才执行下一动作；也可单独点击“重试当前画面”。无效的模型输出不会
自动重试。关闭自动判断后，可以明确选择继续播放动作。

## 故障与恢复

- **下一次抓空**：在抓取前点击，第一次闭合夹爪不会绑定方块。抬起后可以看到空夹爪，
  下一技能变成重新抓取。
- **让方块掉落**：抓住并抬起后点击，解除方块与夹爪的绑定并展示下落，保留重新抓取路径。
- **移动目标托盘**：更换托盘位置，后续放置技能使用新的目标位置。
- **重置场景**：创建新的演示记录，恢复初始姿态和物体位置。

## 模型输入与独立验收

模型读取固定相机的真实 Viser 渲染和任务描述，不会收到物体坐标、关节位置、夹持标志
或场景验收结果。固定相机独立于用户自由旋转的浏览视角；网站中会显示实际发送给模型的图像。

模型返回：

- `placed`：方块是否已放进蓝色托盘并释放。
- `grasped`：方块是否被夹爪夹住并抬离桌面。
- `location`：方块位于桌面、夹爪或托盘的选项概率。

右侧另列出基于演示物体位置与绑定状态计算的场景验收。模型概率高于 0.85 或低于 0.15
时进行明确判断，中间区间显示不确定。概率和验收不一致时会明确显示需要复核，
不会修改概率来迎合场景结果。0.8B 对机器人渲染的判断可能不稳定，应使用实际画面评估。

这是**运动学演示，不是动力学仿真或真机控制**。夹持通过对象绑定展示，下落通过轨迹展示，
没有模拟接触力、摩擦、碰撞或真实抓取稳定性。动作由预设技能执行，OneJev 作为视觉检查器，
当前版本没有将模型输出直接转换成机器人控制指令。

## 三维回放与历史记录

每个已完成动作和故障都会记录姿态，即使没有调用模型。暂停后，点击“动作与故障轨迹”
卡片，三维视图会显示那一刻的机械臂、夹爪、方块和托盘。点击视觉检查缩略图，还能
同时回看当时发送的相机画面、模型概率和独立场景验收。

历史回放只显示记录，不推进当前状态，也不发起模型请求。回放期间执行、故障、重置和
视觉判断按钮会禁用；点击“返回当前场景”后，从原来的动作继续。
回放选择会同步到同一 Studio 的其他标签页。

“当前演示”下拉框可选择之前的记录，包括重置或网站重启前保存的记录；列表显示最近
100 次演示。旧版仅含相机截图的记录可导出 JSON，但不能回放缺失的三维姿态。
“导出 JSON”下载所选演示的完整报告。
快速切换历史记录或返回当前场景时，以最后一次选择为准；较早开始的读取不会重新切换回放。

动作/故障轨迹、视觉截图、原始输出、独立验收和姿态保存在
`runs/studio/robot/<运行 ID>/`。`report.json` 使用原子替换保存，`schema_version: 2`
的 `events` 保存每次动作结束的姿态，`history` 保存视觉检查。
三维回放展示的是这些记录点，并非逐帧动作录像。网站重启会创建新场景，不会自动执行
旧动作；可从历史下拉框回看已保存的场景。
相机截图以无损 PNG 保存，并将同一份图像发送给模型。编码和写盘在后台完成，
不会阻塞状态查询；截图使用较快的压缩设置，文件会略大，像素保持不变。
场景使用固定灯光，避免刚连接相机时因环境贴图尚未加载而得到明显偏暗的首帧。

## 验证

```bash
QEV_BROWSER_TESTS=1 QEV_BROWSER_CHANNEL=chrome python -m pytest -q tests/test_robot.py tests/test_robot_capture.py tests/test_studio_integration.py
```

机器人检查覆盖可达性、完整抓放、抓空后恢复、掉落后恢复、目标移动、无效故障，
以及双标签播放、执行中暂停、调速、过期请求拒绝、回放状态隔离、历史记录，
和真实浏览器中的同源 WebSocket 与相机请求。测试用的模型响应是确定性的，
不需要额外下载模型；真实模型的效果需另行评估。

## English

Install `.[browser,robot]`, run `qev studio`, and open `/#robot`. Viser is managed
automatically and embedded through the website's origin, including its WebSocket.
The workbench shows a procedural six-axis arm performing a scripted pick-and-place task,
with missed-grasp, dropped-object and shifted-target demonstrations.
Playback runs once on the server across tabs, with adjustable speed and dwell time.
Completed actions and faults are recorded even without inference. Replay recorded 3D
poses or archived runs without executing skills or changing the live scene; return to
the live scene to continue. Reports and preferences persist locally across restarts.
Four presets schedule normal placement, missed-grasp recovery, dropped-object recovery
and shifted-target placement; each fault remains a separate observable step. View
settings start hidden on desktop and mobile. Camera reset, viewer reconnect and full
screen operate on the view without resetting the robot. Temporary network failures
disable scene controls until the current run has been synchronized again.

Select a preset and click the one-click full demo to start a fresh run with no
per-step confirmations. Actions, scheduled faults, recovery and enabled visual checks
run automatically; uncertain/disagreeing predictions are recorded without pausing.
Pause and enable pause-on-review for supervised playback from the current state. Execution,
camera and persistent inference errors still stop playback. A managed private viewer renders
observations even after the interactive page closes, rebuilding its connection or browser
when needed. If it cannot launch, an existing interactive viewer is used with an explicit
keep-open message. This automates the demonstration, not model-driven motion planning.

Select the current case or all four presets, choose 1–20 rounds, and start a playlist.
Cases have independent reports and a shared summary with replay links and JSON export.
Pause preserves progress, while ending playback cancels remaining cases and retains
the scene. Archived summaries survive website restarts; execution never resumes by itself.
Transient visual-request failures are retried once without repeating motion. If both
attempts fail, resume first retries the same scene's observation. Failed request evidence
and captured images remain available alongside successful checks.

OneJev receives a fixed-camera render and the task text, while scene acceptance is
displayed independently. Model uncertainty and disagreement stay visible. This is a
kinematic visualization, not contact dynamics or a hardware controller; object attachment
and falling are scripted. The model checks outcomes rather than commanding the arm.
