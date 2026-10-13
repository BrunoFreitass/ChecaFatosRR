"""Versão vigente das páginas legais: pública, para a data exibida e o aceite no cadastro."""

from fastapi import APIRouter, Response
from pydantic import BaseModel

from app.legal import CURRENT_TERMS_VERSION

router = APIRouter(tags=["legal"])


class LegalVersionOut(BaseModel):
    version: str


@router.get("/legal/version", response_model=LegalVersionOut)
async def legal_version(response: Response) -> LegalVersionOut:
    # Sem cache: logo depois de um deploy com Termos novos, quem está no meio do cadastro
    # precisa receber a versão nova ao tentar de novo (senão fica recusado em loop).
    response.headers["Cache-Control"] = "no-cache"
    return LegalVersionOut(version=CURRENT_TERMS_VERSION)
