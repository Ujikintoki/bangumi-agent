"""用户可检查的会话上下文；不推断跨会话画像。"""
import re
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SessionContext(BaseModel):
    model_config = ConfigDict(extra='forbid')
    object_url: str = Field(default='', max_length=300)
    materials: str = Field(default='', max_length=12000)
    corrections: str = Field(default='', max_length=3000)
    preferences: str = Field(default='', max_length=2000)
    progress: str = Field(default='', max_length=1000)

    @field_validator('object_url')
    @classmethod
    def supported_object(cls, value):
        if not value:
            return ''
        parsed = urlparse(value)
        if (parsed.scheme not in {'http', 'https'} or parsed.netloc not in {'bgm.tv', 'bangumi.tv', 'chii.in'}
                or not re.fullmatch(r'/(subject|person|character)/[1-9][0-9]*/?', parsed.path)
                or parsed.query or parsed.fragment):
            raise ValueError('请使用 Bangumi 作品、人物或角色的条目链接')
        return 'https://bgm.tv' + parsed.path.rstrip('/')

    @model_validator(mode='after')
    def bounded_context(self):
        if sum(len(value) for value in self.model_dump().values()) > 8000:
            raise ValueError('会话背景总长最多8000字，请精简引用材料')
        return self

    def snapshot(self):
        snapshot = self.model_dump()
        if self.object_url:
            kind, identifier = urlparse(self.object_url).path.strip('/').split('/')
            snapshot['object'] = {'entity_type': kind, 'entity_id': int(identifier)}
        return snapshot
