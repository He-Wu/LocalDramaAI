# LocalDramaAI 可发布 MVP 设计规格

**日期：** 2026-08-11  
**状态：** 已确认  
**目标版本：** 首个面向 Windows 用户的 GitHub Release  
**首个实施分支：** `feature/pipeline-orchestrator`

## 1. 背景

LocalDramaAI 当前 `main` 分支实现了 Phase 0–7 的基础能力：FastAPI、SQLite WAL、独立 Worker、Ollama 结构化生成、角色与分镜首帧、Qwen3-TTS 配音、Wan2.2 镜头视频，以及相关资产和生成清单持久化。

这些能力尚未形成可交付产品：

- API 只提供项目和任务的最小创建、查询接口。
- Worker 领取任务后仅写入演示状态，没有调度真实生成服务。
- 普通用户只能看到 Swagger，无法完成短剧创作流程。
- Python 包缺少明确的 setuptools 包发现配置，`python -m pip install -e .` 会失败。
- 仓库没有 Windows 应用打包、安装器或 GitHub Release 工作流。

本规格定义一个“可真正生成短剧、可由普通用户操作、可通过 GitHub Releases 安装”的本地 Windows MVP。

## 2. 用户与成功标准

### 2.1 目标用户

- **创作用户：** 粘贴或上传剧本，检查中间结果，生成并下载完整短剧。
- **开发者：** 查看 API、任务输入输出、生成清单和详细日志，能够扩展生成阶段。
- **部署运维人员：** 检查本地依赖、模型和服务健康状态，定位失败阶段并安全重试。

### 2.2 成功标准

一个没有 Python 开发环境的 Windows 用户能够：

1. 从 GitHub Release 下载 ZIP 或图形化安装包。
2. 启动 LocalDramaAI 本地工作台。
3. 完成环境检查并配置 Ollama、ComfyUI、Qwen3-TTS 和 FFmpeg。
4. 粘贴或上传文本剧本。
5. 启动端到端生成任务并查看持久化进度。
6. 检查角色图、分镜、音频和镜头视频。
7. 在失败后从失败阶段重试，或重新生成单个镜头。
8. 导出 H.264/AAC MP4、SRT 字幕和生成清单。

## 3. 范围

### 3.1 本次包含

- Phase 0–7 的端到端生成编排。
- 可恢复、可取消、可重试的阶段状态机。
- 最终镜头音视频合成、顺序拼接和字幕导出。
- React + TypeScript 本地 Web 工作台。
- 面向创作、开发与运维三类用户的分层信息展示。
- Windows x64 免安装 ZIP 和 Inno Setup 安装包。
- `v*` 标签触发的 GitHub Release 流水线。
- 中英文快速开始、安装、使用和故障排除文档。

### 3.2 本次不包含

- MuseTalk 或其他口型同步；Phase 8 作为后续镜头后处理阶段接入。
- 云端部署、多用户协作、账户、鉴权或远程访问。
- 自动下载或打包大模型、ComfyUI、Ollama 或完整 TTS 环境。
- 在线支付、自动更新、遥测或云端素材存储。
- 移动端应用或原生桌面 GUI；桌面入口启动本地 Web 工作台。

## 4. 总体架构

系统保持单机、进程隔离的结构：

```text
Windows 用户
    |
    v
本地 React 工作台  <---->  FastAPI（127.0.0.1）
                              |
                              v
                        SQLite WAL
                              ^
                              |
                        独立 Worker
                              |
             +----------------+----------------+
             |                |                |
          Ollama          Qwen3-TTS         ComfyUI
                                              |
                                           Wan2.2
                              |
                            FFmpeg
                              |
                  MP4 + SRT + 生成清单
```

FastAPI 负责短请求、静态前端、项目数据和状态查询。Worker 独立领取任务并执行阶段状态机。外部 AI 服务保持各自隔离环境，TTS 和 Wan2.2 不同时占用 GPU。

## 5. 子项目与实施顺序

范围拆分为三个可独立验证的子项目：

1. **Pipeline Orchestrator**
   - 真实任务状态机、阶段持久化、取消和恢复。
   - 端到端服务调度、音视频合成和最终导出。
   - 面向工作台的 API 契约。
2. **Web Workbench**
   - React + TypeScript + Vite 本地工作台。
   - 项目、阶段、镜头、运行状态、环境诊断和设置页面。
   - 前端单元测试和关键路径浏览器测试。
3. **Windows Release**
   - Python/前端生产构建、PyInstaller、Inno Setup。
   - ZIP、安装器、校验文件、SBOM 和 GitHub Release。

三个子项目按顺序在独立 worktree 和功能分支完成。发布工作流只在三者均合并到 `main` 后启用。

## 6. Pipeline Orchestrator

### 6.1 任务模型

现有 `GenerationJob` 继续作为顶层任务。新增阶段运行记录，以支持恢复和审计：

- `job_id`：所属任务。
- `stage`：稳定的阶段标识。
- `status`：`pending`、`running`、`completed`、`failed` 或 `cancelled`。
- `attempt`：当前尝试次数。
- `input_json`：该阶段的不可变输入快照。
- `output_json`：已验证输出的引用和摘要。
- `error_code`、`error_message`：面向程序和用户的错误信息。
- `started_at`、`completed_at`：阶段计时。

顶层任务增加取消请求与恢复所需状态，但不把长时间生成放入 API 请求。

### 6.2 阶段顺序

完整短剧任务使用以下稳定阶段标识：

1. `environment_check`
2. `script_structure`
3. `character_master`
4. `storyboard`
5. `dialogue_audio`
6. `release_tts_gpu`
7. `shot_video`
8. `shot_mux`
9. `final_concat`
10. `subtitle_export`
11. `manifest_export`

批量阶段按角色或镜头维护子项进度。一个子项失败时，阶段失败并记录具体角色、镜头或台词 ID。

### 6.3 数据流

1. 用户创建项目并提交剧本文本、画幅和质量档位。
2. Ollama 将剧本转换为现有 `StructuredDrama` 契约。
3. 系统在短事务中保存角色、场景、镜头和台词。
4. 角色 MASTER、分镜首帧、对白 WAV 和镜头视频依次生成并注册为 `Asset`。
5. 每次生成写入 `GenerationManifest`，记录模型、提示词、工作流和输入资产。
6. FFmpeg 为每个镜头校准视频长度并混入对白音频。
7. 已验证镜头按 `scene.order` 和 `shot.order` 拼接。
8. 系统导出 SRT 和项目级 JSON 生成清单。
9. 最终文件验证通过后注册为项目导出资产，并将任务标记为完成。

### 6.4 恢复、重试和取消

- 阶段开始前保存输入快照，完成后保存已验证输出。
- Worker 重启时把孤立的 `running` 阶段恢复为可重试状态。
- 默认重试从首个失败阶段开始，复用全部已完成阶段。
- 单镜头重生成使该镜头及依赖它的 `shot_mux`、`final_concat` 和清单阶段失效。
- 取消请求在阶段安全点检查；外部请求结束或子进程终止后再标记取消完成。
- TTS 阶段结束或失败后都尝试卸载模型；进入 Wan2.2 阶段前再次确认 TTS 已释放。
- 临时文件写入同一目标卷中的临时路径，验证后原子重命名。
- 数据库只引用已验证且已经原子发布的文件。

### 6.5 最终输出

- `final.mp4`：H.264 视频和 AAC 音频。
- `subtitles.srt`：根据持久化对白时长和镜头时间轴生成。
- `generation-manifest.json`：项目、资产、模型、工作流、种子和哈希摘要。

首版不进行口型同步。后续 Phase 8 插入在 `shot_video` 与 `shot_mux` 之间，不改变前序阶段契约。

## 7. API 契约

在保留现有接口的基础上，增加工作台需要的资源：

- 项目列表、详情、删除前检查和项目资产列表。
- 剧本上传/提交与结构化结果读取。
- 完整流水线启动、取消、重试和单镜头重生成。
- 任务阶段、事件和汇总进度查询。
- 角色、场景、镜头、台词及其资产读取。
- 受控的本地媒体访问与最终导出下载。
- 环境健康检查、GPU 状态和非敏感配置读取。
- 配置验证和显式保存。

所有文件访问通过数据库资产 ID 解析，不接受任意文件系统路径。服务仅监听 `127.0.0.1`。

## 8. 本地 Web 工作台

### 8.1 技术结构

- React、TypeScript、Vite。
- 生产构建输出静态资源，由 FastAPI 提供。
- 前端开发服务器只用于开发；发布包不依赖 Node.js。
- MVP 使用短间隔状态查询，避免为本地单用户场景引入额外消息基础设施。

### 8.2 信息架构

采用已确认的混合式工作台：

- **项目首页：** 新建短剧、最近项目、活跃任务和整体环境状态。
- **新建短剧：** 粘贴文本或上传 `.txt`/`.md`，配置画幅、质量和输出位置。
- **项目工作台：** 剧本、角色、分镜、配音、视频、导出六个用户可理解阶段。
- **镜头面板：** 预览首帧、音频和视频，显示状态并允许单镜头重生成。
- **运行面板：** 总进度、当前阶段、GPU/内存、事件日志、取消和失败重试。
- **环境诊断：** FFmpeg、Ollama、ComfyUI、Qwen3-TTS、模型和路径检查。
- **设置：** 服务地址、模型、数据目录和生成档位。

普通用户默认看到任务含义和下一步操作。技术输入、生成清单和详细日志通过可展开区域提供给开发与运维用户。

### 8.3 错误展示

错误消息同时包含：

- 失败阶段和受影响的角色、镜头或台词。
- 简明原因。
- 可执行建议，例如启动服务、修正路径、释放内存或拆分过长镜头。
- “重试本阶段”或“打开环境诊断”等上下文操作。

不得把外部服务不可用显示为生成成功，也不得仅显示无法操作的 Python 堆栈。

## 9. Windows 运行与用户目录

发布包提供：

- `LocalDramaAI-API.exe`
- `LocalDramaAI-Worker.exe`
- `Start-LocalDramaAI.ps1`
- `Stop-LocalDramaAI.ps1`
- `Check-Environment.ps1`

程序与用户数据分离：

```text
%LOCALAPPDATA%\Programs\LocalDramaAI\
%LOCALAPPDATA%\LocalDramaAI\config\.env
%LOCALAPPDATA%\LocalDramaAI\data\
%LOCALAPPDATA%\LocalDramaAI\logs\
%LOCALAPPDATA%\LocalDramaAI\run\
```

首次启动创建缺失的用户配置和目录。升级不得覆盖现有 `.env`、数据库或用户资产。开发环境现有配置行为保持兼容；打包启动入口负责设置发布版默认用户目录。

启动脚本记录本次启动的 PID，只停止属于当前应用实例的进程。启动成功后默认打开工作台首页，Swagger 保留在 `/docs`。

## 10. GitHub Release 流水线

### 10.1 触发与版本

- 推送 `v*` 标签触发正式构建。
- 标签必须指向已经合并到 `main` 的提交。
- 标签、Python 项目版本和应用显示版本必须一致。
- `vX.Y.Z-rc.N` 自动标记为预发布版本。
- 同一标签不得覆盖已有 Release。

### 10.2 构建阶段

1. 后端依赖安装、Pytest、配置和工作流校验。
2. 前端锁定依赖安装、类型检查、单元测试、生产构建和关键路径浏览器测试。
3. PyInstaller `onedir` 构建 API、Worker 和启动入口。
4. 组装统一 staging 目录。
5. 从 staging 目录生成 ZIP。
6. Inno Setup 使用同一 staging 目录生成安装包。
7. 在干净路径执行启动和 `/health`、首页冒烟验证。
8. 生成 SHA256 校验文件和 SPDX JSON SBOM。
9. 创建 GitHub Release 并上传全部制品。

### 10.3 制品

- `LocalDramaAI-vX.Y.Z-windows-x64.zip`
- `LocalDramaAI-Setup-vX.Y.Z.exe`
- `SHA256SUMS.txt`
- `SBOM.spdx.json`

ZIP 和安装器必须来自同一 staging 文件清单。staging 不得包含 `.env`、数据库、凭据、模型、日志、生成媒体、测试缓存或本地绝对路径配置。

### 10.4 发布安全

- 工作流默认只读；发布 Job 单独获得 `contents: write`。
- 第三方 GitHub Actions 固定到明确提交 SHA。
- 不在日志中输出凭据或完整用户配置。
- 预留 Authenticode 条件签名；证书密钥存在时签名，没有证书时明确标记未签名并说明 SmartScreen 提示。
- 不自动删除、覆盖或回滚既有 Release。

## 11. 文档交付

Release 至少包含以下中英文文档：

- 快速开始与首次启动。
- 外部依赖安装和模型准备。
- 创作工作台使用指南。
- 配置项和用户目录说明。
- 常见错误、恢复与重试。
- 开发环境、测试和发布维护指南。

README 作为入口，分别链接用户、开发和运维路径。现有架构、模型、工作流、环境和基准文档继续保留，但修正硬编码路径和过时阶段描述。

## 12. 测试策略

### 12.1 常规 CI 门禁

- 状态机、阶段依赖、取消、恢复和失效传播单元测试。
- 使用伪 Provider 的端到端编排测试。
- API 成功、失败、权限边界和资产路径测试。
- FFmpeg 音视频合成、时长和原子发布测试。
- React 组件、状态和错误操作测试。
- 浏览器关键路径：创建项目、提交剧本、查看阶段、失败重试和下载导出。
- PyInstaller staging 内容和发布包启动冒烟测试。

### 12.2 GPU 验证

GitHub 托管 runner 不承担真实 GPU 模型生成。真实 Ollama、Qwen3-TTS、ComfyUI 和 Wan2.2 smoke 由人工触发的发布候选流程在目标 Windows GPU 主机运行，并把版本、模型哈希、资源峰值和输出验证结果保存为发布证据。

## 13. 已知前置问题

在设计 worktree 建立时，`python -m pip install -e .` 因 setuptools 自动发现多个顶层目录而失败。现有测试基线为 `34 passed`。Pipeline Orchestrator 的首个实施计划必须先明确 Python 包发现范围，并增加安装验证，确保后续 PyInstaller 构建有稳定输入。

## 14. 验收条件

该可发布 MVP 只有在以下条件全部满足后才可打正式标签：

- 全部常规 CI 门禁通过。
- 目标 Windows GPU 主机的发布候选 smoke 通过。
- 普通用户能从工作台完成剧本提交到成片导出。
- 失败阶段可恢复，单镜头可重生成，取消不会遗留错误成功状态。
- ZIP 和安装器在干净用户目录中可启动。
- Release 制品不包含敏感配置、模型或用户数据。
- 中英文用户、开发和运维文档已随制品提供。

## 15. 设计决策摘要

- 使用本地 Web 工作台，不以 Swagger 作为用户界面。
- 使用混合式布局，同时满足创作、开发和运维需求。
- 首版覆盖 Phase 0–7，Phase 8 口型同步延后。
- 使用阶段持久化实现恢复、重试和取消。
- 使用 PyInstaller `onedir` 作为 ZIP 与安装器的共同输入。
- 使用 Inno Setup 生成图形化 Windows 安装包。
- 使用 `v*` 标签触发 GitHub Release。
- 外部 AI 服务与大模型不进入发布制品。
