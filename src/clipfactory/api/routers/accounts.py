"""Account CRUD. Credentials are Fernet-encrypted at rest and never returned."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clipfactory.api.deps import get_db
from clipfactory.api.schemas import AccountCreate, AccountOut, AccountUpdate
from clipfactory.models import Account

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _to_out(account: Account) -> AccountOut:
    return AccountOut(
        id=account.id,
        platform=account.platform,
        name=account.name,
        enabled=account.enabled,
        has_credentials=bool(account.credentials_encrypted),
        created_at=account.created_at,
    )


@router.get("", response_model=list[AccountOut])
def list_accounts(db: Session = Depends(get_db)) -> list[AccountOut]:
    accounts = db.scalars(select(Account).order_by(Account.created_at.desc()))
    return [_to_out(a) for a in accounts]


@router.post("", response_model=AccountOut, status_code=201)
def create_account(payload: AccountCreate, db: Session = Depends(get_db)) -> AccountOut:
    encrypted = ""
    if payload.credentials:
        from clipfactory.crypto import CryptoError, encrypt_credentials

        try:
            encrypted = encrypt_credentials(payload.credentials)
        except CryptoError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    account = Account(platform=payload.platform, name=payload.name, credentials_encrypted=encrypted)
    db.add(account)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Account with this platform+name already exists") from exc
    db.refresh(account)
    return _to_out(account)


@router.patch("/{account_id}", response_model=AccountOut)
def update_account(account_id: int, payload: AccountUpdate, db: Session = Depends(get_db)) -> AccountOut:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    data = payload.model_dump(exclude_unset=True)
    if "enabled" in data:
        account.enabled = data["enabled"]
    if "credentials" in data and data["credentials"] is not None:
        from clipfactory.crypto import CryptoError, encrypt_credentials

        try:
            account.credentials_encrypted = encrypt_credentials(data["credentials"])
        except CryptoError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    db.flush()
    db.refresh(account)
    return _to_out(account)


@router.delete("/{account_id}", status_code=204)
def delete_account(account_id: int, db: Session = Depends(get_db)) -> None:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    db.delete(account)
