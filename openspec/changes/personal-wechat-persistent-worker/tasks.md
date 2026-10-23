# Tasks

## 1. Python 常驻通道

- [x] 1.1 新增 `worker/wechat/stdio.py`：NDJSON 请求循环，按 id 调用 `handle_wechat_action`，stdout 只输出 JSON 响应，异常写 stderr
- [x] 1.2 `worker/main.py` 增加 `--wechat-runtime`，复用常驻 runtime 单例与优雅关闭
- [x] 1.3 新增 `bootstrap` 动作：一次返回 detect/projects/settings/providers（可选 messages），不内联图片
- [x] 1.4 Python 单测：id 关联、坏 JSON、并发读、写串行、bootstrap 字段与不内联图片

## 2. Electron supervisor

- [x] 2.1 新增 `electron/main/services/wechatRuntime.ts`：懒启动、请求关联、超时、重启退避、进程树清理
- [x] 2.2 `MainServices.wechatAction` 优先走 supervisor，失败回退 `WorkerClient`；更新/退出时关闭
- [x] 2.3 暴露运行状态（模式、启动耗时、重启次数、in-flight、最近错误）给渲染层诊断
- [x] 2.4 单测：请求/响应、超时、崩溃重启、fallback、shutdown 清理

## 3. 渲染层首屏

- [x] 3.1 `api` 增加 bootstrap 调用，`useWechatWorkbench` 首屏改为一次请求
- [x] 3.2 首屏失败时回退到现有分请求路径；消息/图片仍按需加载
- [x] 3.3 前端测试：bootstrap 正常、局部失败、分请求 fallback

## 4. 验证与文档

- [x] 4.1 运行前端 test/typecheck/build、Python 全量测试
- [ ] 4.2 打包后在真实应用测量：首次进入、再次进入、图片预览、项目保存/删除的耗时与 worker 启动次数
- [x] 4.3 更新 `docs/electron-migration.md` 与 README 的运行时说明
