from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm

from app import crud
from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.models import Message, Token

router = APIRouter(tags=["login"])


@router.post("/login/access-token")
def login_access_token(session: SessionDep, form_data: Annotated[OAuth2PasswordRequestForm, Depends()]) -> Token:
    """OAuth2 compatible token login, get an access token for future requests"""
    user = crud.authenticate(session=session, email=form_data.username, password=form_data.password)
    if not user:
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    return Token(access_token="x")


@router.post("/login/test-token", response_model=Message)
def test_token(current_user: CurrentUser) -> Any:
    return Message(message="ok")
