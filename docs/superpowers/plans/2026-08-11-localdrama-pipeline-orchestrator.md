# LocalDramaAI Pipeline Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 Phase 0–7 服务串成可恢复、可取消、可重试并能导出完整 MP4/SRT/生成清单的真实短剧任务流水线，同时提供 Web 工作台后续接入所需的 API。

**Architecture:** 保留 `GenerationJob` 作为顶层任务，新增 `JobStage` 持久化每个阶段的状态和输入输出快照。`PipelineOrchestrator` 只负责顺序、状态和错误边界；`DefaultPipelineRuntime` 负责调用 Ollama、ComfyUI、Qwen3-TTS 和 FFmpeg。Worker 继续独立运行，API 只创建、查询、取消和重试任务。

**Tech Stack:** Python 3.11+、FastAPI、SQLAlchemy 2、SQLite WAL、Pydantic 2、HTTPX、Pytest、FFmpeg、Ollama、ComfyUI、Qwen3-TTS。

---

## 文件结构

### 新建文件

- `app/models/job_stage.py`：阶段持久化模型。
- `app/pipeline/__init__.py`：流水线包出口。
- `app/pipeline/contracts.py`：运行上下文与 Runtime 协议。
- `app/pipeline/state.py`：阶段状态、事件、取消和重试事务。
- `app/pipeline/orchestrator.py`：阶段顺序和失败边界。
- `app/pipeline/runtime.py`：Phase 0–7 的真实阶段执行器。
- `app/services/drama_persistence.py`：把 `StructuredDrama` 原子写入领域表。
- `app/services/final_render.py`：镜头混音、拼接、SRT 和项目清单导出。
- `app/api/assets.py`：受控 WAV 上传和项目资产读取。
- `app/api/characters.py`：角色声音配置。
- `tests/test_packaging.py`：包发现配置验证。
- `tests/test_pipeline_models.py`：阶段模型与枚举。
- `tests/test_drama_persistence.py`：结构化剧本持久化。
- `tests/test_pipeline_state.py`：状态机事务。
- `tests/test_pipeline_orchestrator.py`：编排、失败、取消和恢复。
- `tests/test_final_render.py`：FFmpeg 成片和字幕。
- `tests/test_pipeline_runtime.py`：真实阶段执行器的 Provider 隔离测试。
- `tests/test_pipeline_api.py`：创建、查询、取消、重试和资产 API。
- `tests/test_pipeline_worker.py`：Worker 调度真实流水线。

### 修改文件

- `pyproject.toml`：限定 setuptools 包发现，增加测试与上传依赖。
- `app/core/enums.py`：完整短剧类型、阶段和阶段状态。
- `app/core/config.py`：TTS、工作流、ComfyUI 输入和项目输出路径。
- `app/models/job.py`：取消字段和阶段关系。
- `app/models/__init__.py`：导出 `JobStage`。
- `app/schemas/drama.py`：补齐角色视觉信息、镜头所属场景和角色。
- `app/schemas/api.py`：流水线、阶段、取消、重试、资产和声音请求响应。
- `app/providers/ffmpeg_provider.py`：混音和无损顺序拼接。
- `app/workers/job_claim.py`：孤立阶段恢复。
- `app/workers/worker.py`：调用异步编排器。
- `app/api/jobs.py`：流水线任务操作。
- `app/api/projects.py`：项目列表与项目资产入口。
- `app/main.py`：注册新增路由。
- `README.md`、`docs/ARCHITECTURE.md`、`docs/TROUBLESHOOTING.md`：更新运行和恢复说明。

## Task 1: 修复 Python 包发现基线

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/test_packaging.py`

- [ ] **Step 1: 写入失败的包发现配置测试**

```python
# tests/test_packaging.py
from pathlib import Path
import tomllib


def test_setuptools_only_discovers_python_packages():
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    find = config["tool"]["setuptools"]["packages"]["find"]
    assert find["include"] == ["app*", "ai_services*"]
    assert find["exclude"] == ["tests*", "scripts*", "comfyui*", "runtime*", "models*"]
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_packaging.py -q`  
Expected: FAIL，缺少 `tool.setuptools`。

- [ ] **Step 3: 明确 setuptools 包发现与开发依赖**

在 `pyproject.toml` 追加：

```toml
[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.23", "python-multipart>=0.0.9"]

[tool.setuptools.packages.find]
include = ["app*", "ai_services*"]
exclude = ["tests*", "scripts*", "comfyui*", "runtime*", "models*"]
```

并把 `python-multipart>=0.0.9` 加入 `[project].dependencies`，因为发布版 API 需要接收声音参考 WAV。

- [ ] **Step 4: 验证可编辑安装和全量基线**

Run: `python -m pip install -e ".[dev]"`  
Expected: `Successfully installed localdramaai-0.1.0`，不再出现 multiple top-level packages。

Run: `python -m pytest -q`  
Expected: 35 tests passed。

- [ ] **Step 5: 提交**

```powershell
git add pyproject.toml tests/test_packaging.py
git commit -m "build: define Python package discovery"
```

## Task 2: 增加流水线阶段领域模型

**Files:**
- Modify: `app/core/enums.py`
- Create: `app/models/job_stage.py`
- Modify: `app/models/job.py`
- Modify: `app/models/__init__.py`
- Create: `tests/test_pipeline_models.py`

- [ ] **Step 1: 写入阶段顺序和持久化失败测试**

```python
# tests/test_pipeline_models.py
from app.core.enums import PIPELINE_STAGES, JobStatus, JobType, PipelineStage, StageStatus
from app.db.session import create_schema, session_scope
from app.models import GenerationJob, JobStage, Project


def test_full_drama_stage_order_is_stable():
    assert JobType.FULL_DRAMA == "FULL_DRAMA"
    assert PIPELINE_STAGES == [
        PipelineStage.ENVIRONMENT_CHECK,
        PipelineStage.SCRIPT_STRUCTURE,
        PipelineStage.CHARACTER_MASTER,
        PipelineStage.STORYBOARD,
        PipelineStage.DIALOGUE_AUDIO,
        PipelineStage.RELEASE_TTS_GPU,
        PipelineStage.SHOT_VIDEO,
        PipelineStage.SHOT_MUX,
        PipelineStage.FINAL_CONCAT,
        PipelineStage.SUBTITLE_EXPORT,
        PipelineStage.MANIFEST_EXPORT,
    ]


def test_job_stage_round_trip(tmp_path):
    database = str(tmp_path / "pipeline.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="Pipeline")
        session.add(project)
        session.flush()
        job = GenerationJob(project_id=project.id, type=JobType.FULL_DRAMA, status=JobStatus.QUEUED)
        session.add(job)
        session.flush()
        stage = JobStage(job_id=job.id, stage=PipelineStage.ENVIRONMENT_CHECK, status=StageStatus.PENDING)
        session.add(stage)
        session.flush()
        stage_id = stage.id
    with session_scope(database) as session:
        stage = session.get(JobStage, stage_id)
        assert stage.job.type == JobType.FULL_DRAMA
        assert stage.status == StageStatus.PENDING
```

- [ ] **Step 2: 运行测试并确认导入失败**

Run: `python -m pytest tests/test_pipeline_models.py -q`  
Expected: FAIL，无法导入流水线枚举或 `JobStage`。

- [ ] **Step 3: 添加稳定枚举**

在 `app/core/enums.py` 添加：

```python
class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PipelineStage(StrEnum):
    ENVIRONMENT_CHECK = "environment_check"
    SCRIPT_STRUCTURE = "script_structure"
    CHARACTER_MASTER = "character_master"
    STORYBOARD = "storyboard"
    DIALOGUE_AUDIO = "dialogue_audio"
    RELEASE_TTS_GPU = "release_tts_gpu"
    SHOT_VIDEO = "shot_video"
    SHOT_MUX = "shot_mux"
    FINAL_CONCAT = "final_concat"
    SUBTITLE_EXPORT = "subtitle_export"
    MANIFEST_EXPORT = "manifest_export"


PIPELINE_STAGES = list(PipelineStage)
```

并在 `JobType` 中加入：

```python
FULL_DRAMA = "FULL_DRAMA"
```

- [ ] **Step 4: 创建阶段模型**

```python
# app/models/job_stage.py
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import PipelineStage, StageStatus
from .base import Base, TimestampMixin


class JobStage(Base, TimestampMixin):
    __tablename__ = "job_stages"
    __table_args__ = (UniqueConstraint("job_id", "stage", name="uq_job_stage"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True)
    stage: Mapped[PipelineStage] = mapped_column(String(40))
    status: Mapped[StageStatus] = mapped_column(String(20), default=StageStatus.PENDING, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    input_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job = relationship("GenerationJob", back_populates="stages")
```

在 `GenerationJob` 增加：

```python
cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
stages = relationship("JobStage", back_populates="job", cascade="all, delete-orphan", order_by="JobStage.created_at")
```

在 `app/models/__init__.py` 导出：

```python
from .job_stage import JobStage
```

- [ ] **Step 5: 运行模型测试和全量测试**

Run: `python -m pytest tests/test_pipeline_models.py tests/test_core.py -q`  
Expected: 4 tests passed。

- [ ] **Step 6: 提交**

```powershell
git add app/core/enums.py app/models/job_stage.py app/models/job.py app/models/__init__.py tests/test_pipeline_models.py
git commit -m "feat: persist pipeline stage state"
```

## Task 3: 持久化 Ollama 结构化剧本

**Files:**
- Modify: `app/schemas/drama.py`
- Create: `app/services/drama_persistence.py`
- Modify: `app/providers/ollama_provider.py`
- Create: `tests/test_drama_persistence.py`
- Modify: `tests/test_ollama_provider.py`

- [ ] **Step 1: 写入结构化剧本映射测试**

```python
# tests/test_drama_persistence.py
from app.db.session import create_schema, session_scope
from app.models import Character, Dialogue, Project, Scene, Shot
from app.schemas.drama import StructuredDrama
from app.services.drama_persistence import replace_project_drama


def test_replace_project_drama_maps_scene_character_shot_and_dialogue(tmp_path):
    database = str(tmp_path / "drama.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="旧标题", story="雨夜重逢")
        session.add(project)
        session.flush()
        project_id = project.id
    drama = StructuredDrama.model_validate({
        "title": "雨夜重逢",
        "characters": [{
            "name": "林遥", "age": "30岁", "gender": "女", "face": "椭圆脸",
            "eyes": "杏眼", "hair": "黑色短发", "body": "纤细", "clothes": "蓝色夹克",
            "visual_style": "写实", "personality": "冷静"
        }],
        "scenes": [{"order": 1, "title": "雨夜", "description": "路灯下"}],
        "shots": [{
            "order": 1, "scene_order": 1, "character_name": "林遥", "title": "近景",
            "description": "林遥抬头", "video_prompt": "subtle blink"
        }],
        "dialogues": [{"shot_order": 1, "character_name": "林遥", "text": "我回来了。"}]
    })
    replace_project_drama(database, project_id, drama)
    with session_scope(database) as session:
        character = session.query(Character).one()
        scene = session.query(Scene).one()
        shot = session.query(Shot).one()
        dialogue = session.query(Dialogue).one()
        assert character.visual_bible_json["clothes"] == "蓝色夹克"
        assert shot.scene_id == scene.id and shot.character_id == character.id
        assert dialogue.shot_id == shot.id and dialogue.character_id == character.id
```

- [ ] **Step 2: 运行测试并确认 schema 拒绝新字段或服务缺失**

Run: `python -m pytest tests/test_drama_persistence.py -q`  
Expected: FAIL。

- [ ] **Step 3: 扩充结构化 schema**

把 `CharacterSpec` 改为：

```python
class CharacterSpec(BaseModel):
    name: str
    age: str = "成年"
    gender: str = "未知"
    face: str = "自然脸型"
    eyes: str = "自然眼型"
    nose: str = ""
    mouth: str = ""
    hair: str = "自然发型"
    body: str = "自然体型"
    clothes: str = "日常服装"
    accessories: str = ""
    visual_style: str = "写实"
    personality: str = ""
```

在 `ShotSpec` 增加：

```python
scene_order: int = Field(default=1, ge=1)
character_name: str | None = None
```

- [ ] **Step 4: 实现原子领域写入**

```python
# app/services/drama_persistence.py
from sqlalchemy import delete, select

from app.db.session import session_scope
from app.models import Character, Dialogue, Project, Scene, Shot
from app.schemas.character import VisualBible
from app.schemas.drama import StructuredDrama


def replace_project_drama(database_url: str, project_id: str, drama: StructuredDrama) -> dict:
    with session_scope(database_url) as session:
        project = session.get(Project, project_id)
        if project is None:
            raise ValueError("project not found")
        old_scene_ids = list(session.scalars(select(Scene.id).where(Scene.project_id == project_id)))
        if old_scene_ids:
            session.execute(delete(Scene).where(Scene.id.in_(old_scene_ids)))
        session.execute(delete(Character).where(Character.project_id == project_id))

        characters = {}
        for spec in drama.characters:
            bible = VisualBible(
                name=spec.name, age=spec.age, gender=spec.gender, face=spec.face, eyes=spec.eyes,
                nose=spec.nose, mouth=spec.mouth, hair=spec.hair, body=spec.body,
                clothes=spec.clothes, accessories=spec.accessories, visual_style=spec.visual_style,
            )
            character = Character(project_id=project_id, name=spec.name, visual_bible_json=bible.model_dump())
            session.add(character)
            session.flush()
            characters[spec.name] = character

        scenes = {}
        for spec in sorted(drama.scenes, key=lambda item: item.order):
            scene = Scene(
                project_id=project_id, order=spec.order, title=spec.title, description=spec.description,
                location=spec.location, time_of_day=spec.time_of_day, mood=spec.mood,
                estimated_duration=spec.estimated_duration,
            )
            session.add(scene)
            session.flush()
            scenes[spec.order] = scene

        shots = {}
        for spec in sorted(drama.shots, key=lambda item: item.order):
            scene = scenes.get(spec.scene_order)
            if scene is None:
                raise ValueError(f"shot {spec.order} references missing scene {spec.scene_order}")
            character = characters.get(spec.character_name) if spec.character_name else None
            shot = Shot(
                scene_id=scene.id, character_id=character.id if character else None, order=spec.order,
                title=spec.title, description=spec.description, shot_type=spec.shot_type,
                duration=spec.duration, image_prompt=spec.image_prompt, video_prompt=spec.video_prompt,
                negative_prompt=spec.negative_prompt,
            )
            session.add(shot)
            session.flush()
            shots[spec.order] = shot

        for order, spec in enumerate(drama.dialogues, start=1):
            shot = shots.get(spec.shot_order)
            if shot is None:
                raise ValueError(f"dialogue references missing shot {spec.shot_order}")
            character = characters.get(spec.character_name) if spec.character_name else None
            session.add(Dialogue(
                shot_id=shot.id, character_id=character.id if character else None,
                order=order, text=spec.text, emotion=spec.emotion,
            ))
        project.name = drama.title
        project.status = "STRUCTURED"
        session.flush()
        return {
            "characters": len(characters), "scenes": len(scenes),
            "shots": len(shots), "dialogues": len(drama.dialogues),
        }
```

- [ ] **Step 5: 更新 Ollama 系统提示并验证**

系统提示必须明确 `shots.scene_order`、`shots.character_name` 和角色视觉字段。更新两个 Ollama 测试 fixture，使镜头包含 `scene_order`，角色至少包含默认可接受字段。

Run: `python -m pytest tests/test_drama_persistence.py tests/test_ollama_provider.py -q`  
Expected: 4 tests passed。

- [ ] **Step 6: 提交**

```powershell
git add app/schemas/drama.py app/services/drama_persistence.py app/providers/ollama_provider.py tests/test_drama_persistence.py tests/test_ollama_provider.py
git commit -m "feat: persist structured drama domain"
```

## Task 4: 实现阶段状态事务

**Files:**
- Create: `app/pipeline/__init__.py`
- Create: `app/pipeline/state.py`
- Create: `tests/test_pipeline_state.py`

- [ ] **Step 1: 写入初始化、完成、失败、取消和重试测试**

```python
# tests/test_pipeline_state.py
from app.core.enums import PIPELINE_STAGES, JobStatus, JobType, StageStatus
from app.db.session import create_schema, session_scope
from app.models import GenerationJob, JobEvent, JobStage, Project
from app.pipeline.state import PipelineState


def seed(tmp_path):
    database = str(tmp_path / "state.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="State")
        session.add(project)
        session.flush()
        job = GenerationJob(project_id=project.id, type=JobType.FULL_DRAMA, status=JobStatus.QUEUED)
        session.add(job)
        session.flush()
        return database, job.id


def test_state_initializes_ordered_stages_and_records_completion(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    state.start(PIPELINE_STAGES[0], {"checked": True})
    state.complete(PIPELINE_STAGES[0], {"services": 4})
    with session_scope(database) as session:
        stages = session.query(JobStage).filter_by(job_id=job_id).order_by(JobStage.created_at).all()
        assert [item.stage for item in stages] == PIPELINE_STAGES
        assert stages[0].status == StageStatus.COMPLETED
        assert stages[0].attempt == 1
        assert session.query(JobEvent).filter_by(job_id=job_id).count() == 2


def test_failure_cancel_and_retry_are_persisted(tmp_path):
    database, job_id = seed(tmp_path)
    state = PipelineState(database, job_id)
    state.initialize()
    state.start(PIPELINE_STAGES[0], {})
    state.fail(PIPELINE_STAGES[0], "SERVICE_UNAVAILABLE", "Ollama unavailable")
    state.retry_from(PIPELINE_STAGES[0])
    state.request_cancel()
    assert state.cancel_requested() is True
    with session_scope(database) as session:
        stage = session.query(JobStage).filter_by(job_id=job_id, stage=PIPELINE_STAGES[0]).one()
        assert stage.status == StageStatus.PENDING
        assert stage.error_code is None
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_pipeline_state.py -q`  
Expected: FAIL，`PipelineState` 不存在。

- [ ] **Step 3: 实现状态事务**

`PipelineState` 必须提供以下公开接口，并在每个方法内使用独立短事务：

```python
# app/pipeline/state.py
from datetime import datetime, timezone

from sqlalchemy import func

from app.core.enums import PIPELINE_STAGES, JobStatus, PipelineStage, StageStatus
from app.db.session import session_scope
from app.models import GenerationJob, JobEvent, JobStage


def now():
    return datetime.now(timezone.utc)


class PipelineState:
    def __init__(self, database_url: str, job_id: str):
        self.database_url = database_url
        self.job_id = job_id

    def _event(self, session, event_type: str, progress: float, message: str, payload: dict | None = None):
        sequence = session.query(func.coalesce(func.max(JobEvent.sequence), 0)).filter(JobEvent.job_id == self.job_id).scalar() + 1
        session.add(JobEvent(
            job_id=self.job_id, sequence=sequence, event_type=event_type,
            progress=progress, message=message, payload_json=payload,
        ))

    def initialize(self):
        with session_scope(self.database_url) as session:
            job = session.get(GenerationJob, self.job_id)
            if job is None:
                raise ValueError("job not found")
            existing = {row.stage for row in session.query(JobStage).filter_by(job_id=self.job_id)}
            for stage in PIPELINE_STAGES:
                if stage not in existing:
                    session.add(JobStage(job_id=self.job_id, stage=stage, status=StageStatus.PENDING))

    def start(self, stage: PipelineStage, input_json: dict):
        with session_scope(self.database_url) as session:
            row = session.query(JobStage).filter_by(job_id=self.job_id, stage=stage).one()
            row.status = StageStatus.RUNNING
            row.attempt += 1
            row.input_json = input_json
            row.error_code = None
            row.error_message = None
            row.started_at = now()
            job = session.get(GenerationJob, self.job_id)
            job.status = JobStatus.RUNNING
            job.current_stage = stage.value
            index = PIPELINE_STAGES.index(stage)
            job.progress = index / len(PIPELINE_STAGES)
            self._event(session, "stage_started", job.progress, f"Started {stage.value}", {"stage": stage.value})

    def complete(self, stage: PipelineStage, output_json: dict):
        with session_scope(self.database_url) as session:
            row = session.query(JobStage).filter_by(job_id=self.job_id, stage=stage).one()
            row.status = StageStatus.COMPLETED
            row.output_json = output_json
            row.completed_at = now()
            job = session.get(GenerationJob, self.job_id)
            job.progress = (PIPELINE_STAGES.index(stage) + 1) / len(PIPELINE_STAGES)
            self._event(session, "stage_completed", job.progress, f"Completed {stage.value}", {"stage": stage.value})

    def fail(self, stage: PipelineStage, code: str, message: str):
        with session_scope(self.database_url) as session:
            row = session.query(JobStage).filter_by(job_id=self.job_id, stage=stage).one()
            row.status = StageStatus.FAILED
            row.error_code = code
            row.error_message = message
            row.completed_at = now()
            job = session.get(GenerationJob, self.job_id)
            job.status = JobStatus.FAILED
            job.error_code = code
            job.error_message = message
            self._event(session, "stage_failed", job.progress, message, {"stage": stage.value, "code": code})

    def finish(self, output_json: dict):
        with session_scope(self.database_url) as session:
            job = session.get(GenerationJob, self.job_id)
            job.status = JobStatus.COMPLETED
            job.progress = 1.0
            job.current_stage = "complete"
            job.output_json = output_json
            job.completed_at = now()
            self._event(session, "completed", 1.0, "Pipeline completed", output_json)

    def request_cancel(self):
        with session_scope(self.database_url) as session:
            job = session.get(GenerationJob, self.job_id)
            if job is None:
                raise ValueError("job not found")
            job.cancel_requested_at = now()
            self._event(session, "cancel_requested", job.progress, "Cancellation requested")

    def cancel_requested(self) -> bool:
        with session_scope(self.database_url) as session:
            return session.get(GenerationJob, self.job_id).cancel_requested_at is not None

    def mark_cancelled(self, stage: PipelineStage):
        with session_scope(self.database_url) as session:
            row = session.query(JobStage).filter_by(job_id=self.job_id, stage=stage).one()
            row.status = StageStatus.CANCELLED
            row.completed_at = now()
            job = session.get(GenerationJob, self.job_id)
            job.status = JobStatus.CANCELLED
            job.completed_at = now()
            self._event(session, "cancelled", job.progress, f"Cancelled before {stage.value}")

    def retry_from(self, stage: PipelineStage):
        start = PIPELINE_STAGES.index(stage)
        with session_scope(self.database_url) as session:
            for row in session.query(JobStage).filter_by(job_id=self.job_id).all():
                if PIPELINE_STAGES.index(row.stage) >= start:
                    row.status = StageStatus.PENDING
                    row.output_json = None
                    row.error_code = None
                    row.error_message = None
                    row.started_at = None
                    row.completed_at = None
            job = session.get(GenerationJob, self.job_id)
            job.status = JobStatus.QUEUED
            job.current_stage = stage.value
            job.error_code = None
            job.error_message = None
            job.cancel_requested_at = None
            job.retry_count += 1
            self._event(session, "retry_queued", job.progress, f"Retry queued from {stage.value}")
```

`app/pipeline/__init__.py` 导出 `PipelineState`。

- [ ] **Step 4: 运行状态测试**

Run: `python -m pytest tests/test_pipeline_state.py -q`  
Expected: 2 tests passed。

- [ ] **Step 5: 提交**

```powershell
git add app/pipeline tests/test_pipeline_state.py
git commit -m "feat: manage durable pipeline state"
```

## Task 5: 实现可注入的编排器

**Files:**
- Create: `app/pipeline/contracts.py`
- Create: `app/pipeline/orchestrator.py`
- Create: `tests/test_pipeline_orchestrator.py`

- [ ] **Step 1: 写入成功、跳过已完成阶段、失败和取消测试**

```python
# tests/test_pipeline_orchestrator.py
import pytest

from app.core.enums import PIPELINE_STAGES, JobStatus, JobType, PipelineStage
from app.db.session import create_schema, session_scope
from app.models import GenerationJob, Project
from app.pipeline.orchestrator import PipelineOrchestrator


class FakeRuntime:
    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at

    async def execute(self, stage, context):
        self.calls.append(stage)
        if stage == self.fail_at:
            raise RuntimeError("provider failed")
        return {"stage": stage.value}


def seed(tmp_path):
    database = str(tmp_path / "orchestrator.db")
    create_schema(database)
    with session_scope(database) as session:
        project = Project(name="Run", story="故事")
        session.add(project)
        session.flush()
        job = GenerationJob(project_id=project.id, type=JobType.FULL_DRAMA, status=JobStatus.QUEUED)
        session.add(job)
        session.flush()
        return database, job.id, project.id


@pytest.mark.anyio
async def test_orchestrator_runs_all_stages(tmp_path):
    database, job_id, project_id = seed(tmp_path)
    runtime = FakeRuntime()
    await PipelineOrchestrator(database, runtime).run(job_id)
    assert runtime.calls == PIPELINE_STAGES
    with session_scope(database) as session:
        job = session.get(GenerationJob, job_id)
        assert job.status == JobStatus.COMPLETED
        assert job.output_json["project_id"] == project_id


@pytest.mark.anyio
async def test_orchestrator_persists_failure_boundary(tmp_path):
    database, job_id, _ = seed(tmp_path)
    runtime = FakeRuntime(fail_at=PipelineStage.STORYBOARD)
    await PipelineOrchestrator(database, runtime).run(job_id)
    with session_scope(database) as session:
        job = session.get(GenerationJob, job_id)
        assert job.status == JobStatus.FAILED
        assert job.current_stage == PipelineStage.STORYBOARD
        assert job.error_code == "RUNTIME_ERROR"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_pipeline_orchestrator.py -q`  
Expected: FAIL，缺少编排器。

- [ ] **Step 3: 定义上下文与 Runtime 协议**

```python
# app/pipeline/contracts.py
from dataclasses import dataclass
from typing import Protocol

from app.core.enums import PipelineStage


@dataclass(frozen=True)
class PipelineContext:
    database_url: str
    job_id: str
    project_id: str
    input_json: dict


class PipelineRuntime(Protocol):
    async def execute(self, stage: PipelineStage, context: PipelineContext) -> dict:
        raise NotImplementedError
```

- [ ] **Step 4: 实现编排边界**

```python
# app/pipeline/orchestrator.py
from app.core.enums import PIPELINE_STAGES, JobStatus, PipelineStage, StageStatus
from app.db.session import session_scope
from app.models import GenerationJob, JobStage
from app.pipeline.contracts import PipelineContext, PipelineRuntime
from app.pipeline.state import PipelineState


class PipelineOrchestrator:
    def __init__(self, database_url: str, runtime: PipelineRuntime):
        self.database_url = database_url
        self.runtime = runtime

    async def run(self, job_id: str):
        state = PipelineState(self.database_url, job_id)
        state.initialize()
        with session_scope(self.database_url) as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise ValueError("job not found")
            context = PipelineContext(
                database_url=self.database_url, job_id=job.id, project_id=job.project_id,
                input_json=job.input_json or {},
            )
        last_output = {"project_id": context.project_id}
        for stage in PIPELINE_STAGES:
            with session_scope(self.database_url) as session:
                row = session.query(JobStage).filter_by(job_id=job_id, stage=stage).one()
                if row.status == StageStatus.COMPLETED:
                    continue
            if state.cancel_requested():
                state.mark_cancelled(stage)
                return
            state.start(stage, {"project_id": context.project_id, **context.input_json})
            try:
                output = await self.runtime.execute(stage, context)
            except Exception as exc:
                code = getattr(exc, "code", "RUNTIME_ERROR")
                state.fail(stage, code, str(exc))
                return
            state.complete(stage, output)
            last_output = output
        state.finish({"project_id": context.project_id, **last_output})
```

- [ ] **Step 5: 运行编排器测试**

Run: `python -m pytest tests/test_pipeline_orchestrator.py -q`  
Expected: 2 tests passed。

- [ ] **Step 6: 提交**

```powershell
git add app/pipeline/contracts.py app/pipeline/orchestrator.py tests/test_pipeline_orchestrator.py
git commit -m "feat: orchestrate resumable pipeline stages"
```

## Task 6: 扩展 FFmpeg 镜头混音与拼接

**Files:**
- Modify: `app/providers/ffmpeg_provider.py`
- Create: `tests/test_final_render.py`

- [ ] **Step 1: 写入真实 FFmpeg 混音和拼接测试**

测试用 Pillow 生成两个 640×368 帧，用标准库 `wave` 生成两个短 WAV，通过现有 `image_to_mp4` 创建视频，再验证：

```python
def test_mux_and_concat_publish_valid_h264_aac(tmp_path):
    provider = FFmpegProvider()
    video_a, audio_a = media_pair(tmp_path, "a", (30, 60, 90))
    video_b, audio_b = media_pair(tmp_path, "b", (90, 60, 30))
    mux_a = provider.mux_audio(video_a, audio_a, tmp_path / "mux-a.mp4")
    mux_b = provider.mux_audio(video_b, audio_b, tmp_path / "mux-b.mp4")
    final = provider.concat([mux_a, mux_b], tmp_path / "final.mp4")
    info = probe_video(final)
    assert info.codec == "h264"
    assert info.duration >= 1.9
    assert not list(tmp_path.glob("*.tmp.mp4"))
```

`media_pair` 必须创建 1 秒、24 kHz、单声道 PCM WAV 和对应 1 秒 H.264 MP4。

- [ ] **Step 2: 运行测试并确认方法缺失**

Run: `python -m pytest tests/test_final_render.py::test_mux_and_concat_publish_valid_h264_aac -q`  
Expected: FAIL，`FFmpegProvider` 没有 `mux_audio`。

- [ ] **Step 3: 实现原子混音和 concat demuxer**

在 `FFmpegProvider` 增加：

```python
    def _run(self, args: list[str], timeout: int = 600):
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {result.stderr[-1200:]}")

    def mux_audio(self, video_path: Path, audio_path: Path, output_path: Path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp = output_path.with_name(output_path.stem + ".tmp" + output_path.suffix)
        self._run([
            self.executable, "-y", "-i", str(video_path), "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac",
            "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(temp),
        ])
        temp.replace(output_path)
        return output_path

    def concat(self, inputs: list[Path], output_path: Path):
        if not inputs:
            raise ValueError("at least one video is required")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = output_path.with_suffix(".concat.txt")
        manifest.write_text("".join(f"file '{path.resolve().as_posix()}'\n" for path in inputs), encoding="utf-8")
        temp = output_path.with_name(output_path.stem + ".tmp" + output_path.suffix)
        try:
            self._run([
                self.executable, "-y", "-f", "concat", "-safe", "0", "-i", str(manifest),
                "-c", "copy", "-movflags", "+faststart", str(temp),
            ])
            temp.replace(output_path)
            return output_path
        finally:
            manifest.unlink(missing_ok=True)
            temp.unlink(missing_ok=True)
```

让现有 `_convert` 调用 `_run(args, timeout=300)`，避免重复错误处理。

- [ ] **Step 4: 运行 FFmpeg 测试**

Run: `python -m pytest tests/test_final_render.py::test_mux_and_concat_publish_valid_h264_aac tests/test_phase5.py tests/test_phase7.py -q`  
Expected: 全部通过。

- [ ] **Step 5: 提交**

```powershell
git add app/providers/ffmpeg_provider.py tests/test_final_render.py
git commit -m "feat: mux and concatenate shot media"
```

## Task 7: 导出最终视频、字幕和项目清单

**Files:**
- Create: `app/services/final_render.py`
- Modify: `tests/test_final_render.py`

- [ ] **Step 1: 写入 SRT 时间线和资产注册测试**

测试数据库保存两个有 `video_asset_id`、对白 WAV 与时长的镜头，调用 `FinalRenderService` 后断言：

```python
result = FinalRenderService(database, FFmpegProvider(), tmp_path / "exports").render(project_id)
assert result["video_asset_id"]
assert result["subtitle_asset_id"]
assert result["manifest_asset_id"]
assert "00:00:00,000 --> 00:00:01,000" in Path(result["subtitle_path"]).read_text(encoding="utf-8-sig")
with session_scope(database) as session:
    assert session.get(Asset, result["video_asset_id"]).kind == "FINAL_VIDEO"
```

- [ ] **Step 2: 运行测试并确认服务缺失**

Run: `python -m pytest tests/test_final_render.py -q`  
Expected: FAIL，无法导入 `FinalRenderService`。

- [ ] **Step 3: 实现分阶段导出接口**

`FinalRenderService` 提供四个幂等方法：

```python
class FinalRenderService:
    def __init__(self, database_url: str, ffmpeg: FFmpegProvider, output_dir: Path):
        self.database_url = database_url
        self.ffmpeg = ffmpeg
        self.output_dir = Path(output_dir)

    def mux_shots(self, project_id: str) -> dict:
        """按 scene.order、shot.order 混入该镜头的对白 WAV，返回 muxed_paths。"""

    def concat_project(self, project_id: str, muxed_paths: list[str]) -> dict:
        """拼接镜头、probe 最终 MP4、注册 FINAL_VIDEO Asset。"""

    def export_subtitles(self, project_id: str) -> dict:
        """按镜头累计时间和 dialogue.duration 写 UTF-8 BOM SRT，注册 SUBTITLE Asset。"""

    def export_manifest(self, project_id: str, final_video_asset_id: str) -> dict:
        """导出项目、镜头、输入资产和 GenerationManifest 摘要，注册 MANIFEST Asset。"""
```

实现必须遵守以下固定规则：

- 每个镜头把按 `Dialogue.order` 排序的 WAV 先用 FFmpeg concat 成一个临时 WAV，再与镜头视频混音。
- 没有对白的镜头生成与镜头等长的静音 AAC 音轨。
- 所有输出写到 `output_dir / project_id`，先写 `.tmp` 再原子替换。
- SRT 序号连续，时间码由持久化真实时长累计，不依赖文件名。
- JSON 清单使用 `ensure_ascii=False`、`indent=2`、排序后的键，并记录 SHA256。
- 数据库注册只发生在文件验证通过之后。

- [ ] **Step 4: 运行完整成片测试**

Run: `python -m pytest tests/test_final_render.py -q`  
Expected: 2 tests passed。

- [ ] **Step 5: 提交**

```powershell
git add app/services/final_render.py tests/test_final_render.py
git commit -m "feat: export final drama assets"
```

## Task 8: 实现 Phase 0–7 默认运行时

**Files:**
- Modify: `app/core/config.py`
- Create: `app/pipeline/runtime.py`
- Create: `tests/test_pipeline_runtime.py`

- [ ] **Step 1: 写入阶段路由和 GPU 顺序测试**

使用 Fake Ollama/Image/TTS/Video/Render 服务，验证：

```python
@pytest.mark.anyio
async def test_runtime_dispatches_all_stages_and_unloads_tts_before_video(runtime, context):
    outputs = {}
    for stage in PIPELINE_STAGES:
        outputs[stage] = await runtime.execute(stage, context)
    assert runtime.trace.index("tts_unload") < runtime.trace.index("video_generate")
    assert outputs[PipelineStage.SCRIPT_STRUCTURE]["shots"] == 1
    assert outputs[PipelineStage.MANIFEST_EXPORT]["manifest_asset_id"]
```

另写环境检查失败测试，Fake Ollama 返回 `False` 时必须抛出带 `code = "OLLAMA_UNAVAILABLE"` 的异常。

- [ ] **Step 2: 运行测试并确认运行时缺失**

Run: `python -m pytest tests/test_pipeline_runtime.py -q`  
Expected: FAIL。

- [ ] **Step 3: 补齐配置**

在 `Settings` 增加：

```python
qwen3_tts_url: str = "http://127.0.0.1:8020"
comfyui_input_dir: Path = Path("E:/LocalDramaAI/ComfyUI/input")
workflow_dir: Path = Path("comfyui/workflows")
project_output_dir: Path | None = None

@property
def resolved_project_output_dir(self) -> Path:
    return self.project_output_dir or self.data_root / "projects"
```

- [ ] **Step 4: 实现显式阶段分派**

```python
# app/pipeline/runtime.py
class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DefaultPipelineRuntime:
    def __init__(self, *, ollama, image_provider, tts_provider, video_provider, render_service_factory,
                 character_workflow, storyboard_workflow, video_workflow, paths):
        self.ollama = ollama
        self.image_provider = image_provider
        self.tts_provider = tts_provider
        self.video_provider = video_provider
        self.render_service_factory = render_service_factory
        self.character_workflow = character_workflow
        self.storyboard_workflow = storyboard_workflow
        self.video_workflow = video_workflow
        self.paths = paths

    async def execute(self, stage, context):
        handlers = {
            PipelineStage.ENVIRONMENT_CHECK: self.environment_check,
            PipelineStage.SCRIPT_STRUCTURE: self.script_structure,
            PipelineStage.CHARACTER_MASTER: self.character_master,
            PipelineStage.STORYBOARD: self.storyboard,
            PipelineStage.DIALOGUE_AUDIO: self.dialogue_audio,
            PipelineStage.RELEASE_TTS_GPU: self.release_tts_gpu,
            PipelineStage.SHOT_VIDEO: self.shot_video,
            PipelineStage.SHOT_MUX: self.shot_mux,
            PipelineStage.FINAL_CONCAT: self.final_concat,
            PipelineStage.SUBTITLE_EXPORT: self.subtitle_export,
            PipelineStage.MANIFEST_EXPORT: self.manifest_export,
        }
        return await handlers[stage](context)
```

各 handler 必须调用现有服务，并返回只包含 JSON 可序列化值的摘要：

- `environment_check`：依次检查 Ollama、ComfyUI、TTS、FFmpeg 和必需 workflow 文件。
- `script_structure`：读取 `Project.story`，调用 `generate_drama`，再调用 `replace_project_drama`。
- `character_master`：按创建顺序调用 `generate_character_master`，已有有效 MASTER 时跳过。
- `storyboard`：按场景/镜头顺序调用 `generate_storyboard`，已有有效首帧时跳过。
- `dialogue_audio`：先验证所有有声角色均存在 `VoiceProfile` 和参考 WAV，再逐句调用 `generate_dialogue_audio`。
- `release_tts_gpu`：在 `finally` 语义下调用 TTS `unload`，失败时阻止进入视频阶段。
- `shot_video`：按镜头顺序调用 `generate_dialogue_video`。
- `shot_mux`、`final_concat`、`subtitle_export`、`manifest_export`：调用同一个项目级 `FinalRenderService`，阶段间通过数据库资产和确定性路径衔接。

提供 `DefaultPipelineRuntime.from_settings(settings)` 工厂，创建 `ComfyUIClient`、所有 Provider、加载三份 workflow JSON，并建立项目输出路径。不得在模块 import 时连接外部服务。

- [ ] **Step 5: 运行 Runtime 隔离测试**

Run: `python -m pytest tests/test_pipeline_runtime.py tests/test_phase4.py tests/test_phase6.py tests/test_phase7.py -q`  
Expected: 全部通过。

- [ ] **Step 6: 提交**

```powershell
git add app/core/config.py app/pipeline/runtime.py tests/test_pipeline_runtime.py
git commit -m "feat: execute Phase 0-7 pipeline runtime"
```

## Task 9: 让 Worker 执行真实流水线并恢复孤立阶段

**Files:**
- Modify: `app/workers/job_claim.py`
- Modify: `app/workers/worker.py`
- Modify: `app/worker_main.py`
- Create: `tests/test_pipeline_worker.py`
- Modify: `tests/test_worker.py`

- [ ] **Step 1: 写入 Worker 调度测试**

```python
@pytest.mark.anyio
async def test_worker_runs_claimed_full_drama_job(tmp_path):
    database, job_id = seed_full_drama_job(tmp_path)
    runtime = FakeRuntime()
    worker = LocalDramaWorker(database_url=database, worker_id="worker-a", runtime_factory=lambda: runtime)
    assert await worker.process_one() is True
    with session_scope(database) as session:
        assert session.get(GenerationJob, job_id).status == JobStatus.COMPLETED


def test_recovery_marks_running_stage_failed(tmp_path):
    database, job_id = seed_running_job_and_stage(tmp_path, worker_id="dead-worker")
    assert recover_orphaned_jobs(database, {"live-worker"}) == 1
    with session_scope(database) as session:
        stage = session.query(JobStage).filter_by(job_id=job_id).one()
        assert stage.status == StageStatus.FAILED
        assert stage.error_code == "WORKER_INTERRUPTED"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/test_pipeline_worker.py tests/test_worker.py -q`  
Expected: FAIL，`process_one` 仍是同步占位实现。

- [ ] **Step 3: 改造 Worker**

`LocalDramaWorker` 接收 `runtime_factory`，`process_one` 改为异步：

```python
async def process_one(self):
    job = claim_next_job(self.database_url, self.worker_id)
    if job is None:
        return False
    if job.type != JobType.FULL_DRAMA:
        with session_scope(self.database_url) as session:
            db_job = session.get(GenerationJob, job.id)
            db_job.status = JobStatus.FAILED
            db_job.error_code = "UNSUPPORTED_JOB_TYPE"
            db_job.error_message = f"Unsupported job type: {job.type}"
        return True
    runtime = self.runtime_factory()
    await PipelineOrchestrator(self.database_url, runtime).run(job.id)
    return True
```

`run` 循环改为 `if not await self.process_one()`。默认 factory 使用 `DefaultPipelineRuntime.from_settings(settings)`。

`recover_orphaned_jobs` 在把顶层任务标记 `INTERRUPTED` 时，同时把该任务 `RUNNING` 的 `JobStage` 标记为 `FAILED`，写入 `WORKER_INTERRUPTED`，保留已完成阶段。

- [ ] **Step 4: 运行 Worker 测试**

Run: `python -m pytest tests/test_pipeline_worker.py tests/test_worker.py -q`  
Expected: 全部通过。

- [ ] **Step 5: 提交**

```powershell
git add app/workers/job_claim.py app/workers/worker.py app/worker_main.py tests/test_pipeline_worker.py tests/test_worker.py
git commit -m "feat: run pipeline jobs in worker"
```

## Task 10: 提供任务查询、取消和重试 API

**Files:**
- Modify: `app/schemas/api.py`
- Modify: `app/api/jobs.py`
- Modify: `app/api/projects.py`
- Create: `tests/test_pipeline_api.py`

- [ ] **Step 1: 写入 API 失败测试**

使用 `TestClient` 和临时数据库覆盖 `jobs.settings.database_url`，覆盖：

```python
def test_create_full_drama_job_initializes_queue(client, project_id):
    response = client.post("/api/jobs", json={"project_id": project_id, "type": "FULL_DRAMA", "input_json": {"quality": "draft"}})
    assert response.status_code == 201
    assert response.json()["status"] == "QUEUED"


def test_job_detail_cancel_and_retry(client, failed_job_id):
    detail = client.get(f"/api/jobs/{failed_job_id}")
    assert len(detail.json()["stages"]) == 11
    assert client.post(f"/api/jobs/{failed_job_id}/cancel").status_code == 202
    retry = client.post(f"/api/jobs/{failed_job_id}/retry", json={"stage": "storyboard"})
    assert retry.status_code == 202
    assert retry.json()["status"] == "QUEUED"
```

另测项目不存在、空剧本、运行中任务禁止重试和已完成任务禁止取消。

- [ ] **Step 2: 运行 API 测试并确认失败**

Run: `python -m pytest tests/test_pipeline_api.py -q`  
Expected: FAIL。

- [ ] **Step 3: 添加 Pydantic 契约**

```python
class JobStageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    stage: PipelineStage
    status: StageStatus
    attempt: int
    output_json: dict | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None


class JobDetailRead(JobRead):
    stages: list[JobStageRead]


class JobRetryRequest(BaseModel):
    stage: PipelineStage


class JobActionRead(BaseModel):
    id: str
    status: JobStatus
```

- [ ] **Step 4: 实现 API 状态规则**

- `POST /api/jobs`：确认项目存在；`FULL_DRAMA` 必须有非空 `Project.story`；创建 queued 事件。
- `GET /api/jobs/{id}`：返回 `JobDetailRead` 和排序阶段。
- `POST /api/jobs/{id}/cancel`：只允许 `QUEUED`、`CLAIMED`、`RUNNING`、`INTERRUPTED`，调用 `PipelineState.request_cancel()`，返回 202。
- `POST /api/jobs/{id}/retry`：只允许 `FAILED`、`INTERRUPTED`、`CANCELLED`，调用 `retry_from()`，返回 202。
- `GET /api/projects`：按 `updated_at DESC` 返回项目列表，为工作台准备入口。

所有 404、409 和 422 响应使用稳定英文错误代码和可读 message，不返回堆栈。

- [ ] **Step 5: 运行 API 与现有核心测试**

Run: `python -m pytest tests/test_pipeline_api.py tests/test_core.py -q`  
Expected: 全部通过。

- [ ] **Step 6: 提交**

```powershell
git add app/schemas/api.py app/api/jobs.py app/api/projects.py tests/test_pipeline_api.py
git commit -m "feat: expose pipeline control API"
```

## Task 11: 提供受控声音参考上传与角色声音配置

**Files:**
- Create: `app/api/assets.py`
- Create: `app/api/characters.py`
- Modify: `app/schemas/api.py`
- Modify: `app/main.py`
- Modify: `tests/test_pipeline_api.py`

- [ ] **Step 1: 写入 WAV 上传和声音配置测试**

```python
def test_upload_wav_and_create_voice_profile(client, project_id, character_id, wav_bytes):
    upload = client.post(
        f"/api/projects/{project_id}/assets/audio",
        files={"file": ("voice.wav", wav_bytes, "audio/wav")},
    )
    assert upload.status_code == 201
    asset_id = upload.json()["id"]
    profile = client.post(
        f"/api/characters/{character_id}/voice-profiles",
        json={"name": "主声音", "reference_asset_id": asset_id, "reference_transcript": "你好"},
    )
    assert profile.status_code == 201
    assert profile.json()["reference_asset_id"] == asset_id
```

另测非 WAV、空文件、跨项目资产和缺少 transcript 均被拒绝。

- [ ] **Step 2: 运行测试并确认路由不存在**

Run: `python -m pytest tests/test_pipeline_api.py -q`  
Expected: FAIL，404。

- [ ] **Step 3: 实现安全上传**

`assets.py` 必须：

- 根据数据库查找项目，不接受客户端文件路径。
- 只接受 `.wav` 和 `audio/wav`/`audio/x-wav`。
- 流式写入 `settings.data_root / "projects" / project_id / "uploads"` 下的 UUID 文件。
- 限制 25 MiB；超限时删除临时文件。
- 写入 `.tmp` 后调用 `probe_wav`，成功后原子改名并注册 `AUDIO_REFERENCE` Asset。
- 提供 `GET /api/projects/{project_id}/assets`，只返回该项目资产元数据，不暴露任意目录浏览。

`characters.py` 验证角色、资产属于同一项目后创建 `VoiceProfile`。默认 model 为 `Qwen3-TTS-12Hz-0.6B-Base`、language 为 `Chinese`。

- [ ] **Step 4: 注册路由并运行 API 测试**

Run: `python -m pytest tests/test_pipeline_api.py -q`  
Expected: 全部通过。

- [ ] **Step 5: 提交**

```powershell
git add app/api/assets.py app/api/characters.py app/schemas/api.py app/main.py tests/test_pipeline_api.py
git commit -m "feat: manage voice reference assets"
```

## Task 12: 完成端到端无 GPU 验证与文档

**Files:**
- Create: `tests/test_pipeline_end_to_end.py`
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/TROUBLESHOOTING.md`

- [ ] **Step 1: 写入端到端 Fake Provider 测试**

测试使用真实 SQLite、Worker、编排器、Pillow/FFmpeg 小媒体和 Fake Ollama/Comfy/TTS Provider：

```python
@pytest.mark.anyio
async def test_full_drama_job_reaches_verified_exports(tmp_path):
    app = build_test_pipeline(tmp_path)
    job_id = app.seed_project_story_voice_and_job()
    assert await app.worker.process_one() is True
    with session_scope(app.database_url) as session:
        job = session.get(GenerationJob, job_id)
        stages = session.query(JobStage).filter_by(job_id=job_id).all()
        assert job.status == JobStatus.COMPLETED
        assert all(stage.status == StageStatus.COMPLETED for stage in stages)
        assert session.query(Asset).filter_by(kind="FINAL_VIDEO").count() == 1
        assert session.query(Asset).filter_by(kind="SUBTITLE").count() == 1
        assert session.query(Asset).filter_by(kind="MANIFEST").count() == 1
```

再增加一个在 `storyboard` 首次失败、调用 `retry_from(STORYBOARD)` 后成功的测试，断言 `script_structure` 的 attempt 仍为 1，`storyboard` 的 attempt 为 2。

- [ ] **Step 2: 运行端到端测试并修复集成缝隙**

Run: `python -m pytest tests/test_pipeline_end_to_end.py -q`  
Expected: 2 tests passed。

修复仅限本计划引入的类型、事务和路径衔接，不进行无关重构。

- [ ] **Step 3: 更新运行文档**

README 增加：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
python -m app.worker_main
```

并提供 `POST /api/projects`、上传声音参考、`POST /api/jobs` 使用 `FULL_DRAMA`、查询阶段、取消与重试的完整 curl/PowerShell 示例。

`docs/ARCHITECTURE.md` 记录 `GenerationJob -> JobStage -> JobEvent` 和外部 Provider 边界。`docs/TROUBLESHOOTING.md` 增加 `OLLAMA_UNAVAILABLE`、`VOICE_PROFILE_MISSING`、`WORKER_INTERRUPTED`、FFmpeg 失败和从阶段重试的处理方法。

- [ ] **Step 4: 运行最终验证**

Run: `python -m pip install -e ".[dev]"`  
Expected: 安装成功。

Run: `python -m pytest -q`  
Expected: 全部测试通过。

Run: `python -m compileall -q app ai_services`  
Expected: exit 0，无输出。

Run: `git diff --check main...HEAD`  
Expected: exit 0，无空白错误。

- [ ] **Step 5: 提交**

```powershell
git add tests/test_pipeline_end_to_end.py README.md docs/ARCHITECTURE.md docs/TROUBLESHOOTING.md
git commit -m "test: verify end-to-end drama pipeline"
```

## 实施完成后的门禁

- `python -m pip install -e ".[dev]"` 成功。
- 全量 Pytest 通过。
- 编排器测试证明成功、失败、取消、孤立恢复和阶段重试。
- Fake Provider 端到端测试产生经过 probe 的最终 MP4、SRT 和项目清单。
- 没有外部服务时，错误被持久化为明确代码，不产生伪成功资产。
- `main` 现有 Phase 4–7 测试继续通过。
- 此分支不包含 React 工作台、PyInstaller、Inno Setup 或 GitHub Actions；这些由后续两个计划完成。
