"""Cloud write receipts only. Never store app secrets or access tokens."""
from datetime import datetime
from sqlmodel import SQLModel,Field

class CloudStorageWrite(SQLModel,table=True):
    id:str=Field(primary_key=True)
    root_key:str=Field(index=True)
    folder_token:str
    name:str
    kind:str
    expected_sha256:str=''
    size:int=0
    status:str='running'  # running | done | unknown | rejected
    file_token:str=''
    error_code:str=''
    attempts:int=1
    created_at:datetime=Field(default_factory=datetime.utcnow)
    updated_at:datetime=Field(default_factory=datetime.utcnow)
