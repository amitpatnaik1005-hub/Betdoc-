import logging

from fastapi import APIRouter

from app.api.deps import CurrentUser
from app.domain.oracle import AshokaOracle, OracleContext, OracleResponse

ashoka_log = logging.getLogger("betdoc.ashoka")

router = APIRouter(tags=["oracle"])

_oracle = AshokaOracle()


@router.post("/suggest", response_model=OracleResponse)
async def suggest(context: OracleContext, current_user: CurrentUser) -> OracleResponse:
    ashoka_log.info(
        "ASHOKA suggest requested by user=%s with %d value bets",
        current_user.id, len(context.available_value_bets),
    )
    return _oracle.generate_suggestions(context)
