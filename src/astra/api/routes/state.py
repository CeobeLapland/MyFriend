"""``GET /v1/state`` —— 角色状态的只读快照（M3）。

**只读，没有写入端点**：`docs/03` §5 明文要求"状态变化应来自事件，而不是 LLM 自己
直接写数据库"。客户端（控制台、将来的状态 UI）只能看。

`docs/04` §2 没有定义这个端点，但三处都需要它：M3 验收要能"看见" happiness 上升、
`docs/01` P1 要"角色状态 UI（情绪、当前行为、设备连接状态）"、开发者控制台需要一个
读取点。记在 ADR 0020。

返回的是**已应用惰性衰减**的有效状态，而不是库里那个"锚点"——客户端看到的就是
模型实际看到的那份。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from astra.api.dependencies import get_state_service
from astra.api.schemas import StateBody
from astra.application.state_service import StateService

router = APIRouter(prefix="/v1", tags=["state"])

StateDep = Annotated[StateService, Depends(get_state_service)]


@router.get("/state", response_model=StateBody, summary="读取角色状态快照")
async def read_state(state: StateDep) -> StateBody:
    snapshot = await state.load_snapshot()

    return StateBody(
        companion_id=state.companion_id,
        emotion=snapshot.emotion.as_dict(),
        relationship=snapshot.relationship.as_dict(),
        emotion_updated_at=snapshot.emotion_updated_at,
        relationship_updated_at=snapshot.relationship_updated_at,
    )
