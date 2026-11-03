# Viser 机器人动作展示

机器人工作台已集成到 OneJev Studio。日常仍然只启动 `qev studio`，然后打开
**http://127.0.0.1:7860/#robot**，或在网站左侧选择“机器人动作”。

## 首次安装

在网站的 Python 环境中安装可选依赖：

```bash
pip install -e '.[browser,robot]'
qev studio
```

Viser 在首次打开机器人页时由 Studio 自动启动和关闭。三维页面与 WebSocket
都通过网站同一个地址提供；SSH 转发时只需要转发网站端口，不需要单独开放 Viser 端口。

## 案例：红色方块放入蓝色托盘

场景使用程序生成的简化六轴机械臂和平行夹爪，不需要下载额外的机器人模型。
可用鼠标旋转、缩放和平移三维视图。

“执行下一动作”会依次运行靠近、闭合夹爪、抬起、放入托盘和收回机械臂。
逆运动学计算目标关节位置，动作以平滑关节插值展示。开启“动作后自动判断”时，
每个技能结束后会请求固定相机渲染，并调用已有 OneJev 服务。

“让模型看一眼”只进行视觉判断，不执行任何动作。“自动演示”在每个技能之间
停留 3 秒；暂停会在当前技能结束后生效。如果判断不确定、与场景验收冲突，或
模型请求失败，自动演示会暂停。仍可手动检查画面和执行技能。

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

视觉检查截图、原始输出、独立验收和关节/物体状态保存在
`runs/studio/robot/<运行 ID>/`。页面缩略图用于回看相机记录，三维视图仍显示当前场景。
该版本的机器人场景在同一 Studio 的多个标签页之间共享。

## 验证

```bash
QEV_BROWSER_TESTS=1 QEV_BROWSER_CHANNEL=chrome python -m unittest discover -s tests -v
```

机器人检查覆盖可达性、完整抓放、抓空后恢复、掉落后恢复、目标移动、无效故障，
以及真实浏览器中的同源 WebSocket 和相机请求。测试用的模型响应是确定性的，
不需要额外下载模型；真实模型的效果需另行评估。

## English

Install `.[browser,robot]`, run `qev studio`, and open `/#robot`. Viser is managed
automatically and embedded through the website's origin, including its WebSocket.
The workbench shows a procedural six-axis arm performing a scripted pick-and-place task,
with missed-grasp, dropped-object and shifted-target demonstrations.

OneJev receives a fixed-camera render and the task text, while scene acceptance is
displayed independently. Model uncertainty and disagreement stay visible. This is a
kinematic visualization, not contact dynamics or a hardware controller; object attachment
and falling are scripted. The model checks outcomes rather than commanding the arm.
