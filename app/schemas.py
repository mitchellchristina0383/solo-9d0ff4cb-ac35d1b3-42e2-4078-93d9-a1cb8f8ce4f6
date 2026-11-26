"""FastAPI request/response schemas and input normalization."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .ownership import image_field

CONTAINER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,252}$")


class ClaimRequest(BaseModel):
    """A manager's complete declaration of fields it currently claims.

    Fields omitted from this declaration that the manager currently owns are
    interpreted as an exit from those fields. Explicit ``null`` is treated the
    same way as omission for an owned field.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        json_schema_extra={
            "examples": [
                {
                    "manager": "app-publisher",
                    "deployment": "shared-web",
                    "replicas": 3,
                    "images": {"web": "registry.example/web:1.27.0"},
                    "takeover": False,
                },
                {
                    "manager": "platform-publisher",
                    "deployment": "shared-web",
                    "replicas": 3,
                    "images": {"web": "registry.example/web:1.27.0"},
                },
                {
                    "manager": "platform-publisher",
                    "deployment": "shared-web",
                    "takeover": True,
                    "replicas": 5,
                },
                {
                    "manager": "app-publisher",
                    "deployment": "shared-web",
                },
            ]
        },
    )

    manager: str = Field(
        ...,
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices(
            "manager",
            "manager_name",
            "manager_id",
            "publisher",
            "owner",
            "owner_id",
            "managed_by",
            "claimed_by",
        ),
        description="发布器/管理方名称。同值声明会把该管理方加入共管列表。",
    )
    deployment: str = Field(
        default="default",
        min_length=1,
        max_length=64,
        validation_alias=AliasChoices(
            "deployment",
            "deployment_name",
            "deployment_id",
            "name",
            "workload",
        ),
        description="演练用 Deployment 标识；本服务是有状态演练台，不访问真实集群。",
    )
    replicas: int | None = Field(
        default=None,
        ge=0,
        description="副本数。省略或传 null 且本方当前管理该字段时，本方退出副本数字段。",
    )
    images: dict[str, str | None] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "images", "container_images", "images_by_container"
        ),
        description=(
            "按容器名区分的镜像。未出现或值为 null 的容器字段，"
            "在本方当前管理时视为本方退出该镜像字段。"
        ),
    )
    takeover: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "takeover", "take_over", "explicit_takeover", "force", "override"
        ),
        description="显式接管。为 true 时允许改动与他方冲突的值，相关字段随后只归提交方。",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_request_shapes(cls, data: Any) -> Any:
        """Accept common wrapped and Kubernetes-container-list forms."""
        if not isinstance(data, dict):
            return data

        # Explicit declaration wrappers. Unknown inner fields are still caught
        # by Pydantic's extra="forbid" validation after flattening.
        for wrapper_name in ("declaration", "claim", "payload"):
            wrapped = data.pop(wrapper_name, None)
            if isinstance(wrapped, dict):
                for key, value in wrapped.items():
                    data.setdefault(key, value)

        # Kubernetes-style spec. Only the supported fields are extracted; all
        # other manifest fields are intentionally outside this rehearsal.
        spec = data.pop("spec", None)
        k8s_containers: Any = None
        if isinstance(spec, dict):
            if "replicas" in spec:
                data.setdefault("replicas", spec["replicas"])
            if isinstance(spec.get("images"), dict):
                data.setdefault("images", spec["images"])

            template = spec.get("template")
            if isinstance(template, dict):
                template_spec = template.get("spec")
                if isinstance(template_spec, dict):
                    k8s_containers = template_spec.get("containers")

        images = data.get("images")
        if images is None:
            for alias in ("container_images", "images_by_container"):
                candidate = data.pop(alias, None)
                if isinstance(candidate, dict):
                    images = candidate
                    break
        else:
            data.pop("container_images", None)
            data.pop("images_by_container", None)

        containers = data.pop("containers", None)
        if containers is None and k8s_containers is not None:
            containers = k8s_containers

        if isinstance(images, list):
            containers = images
            images = None

        if isinstance(containers, list):
            image_map = dict(images) if isinstance(images, dict) else {}
            for item in containers:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("containerName")
                image = item.get("image")
                if isinstance(name, str) and isinstance(image, str):
                    image_map[name] = image
            data["images"] = image_map
        elif isinstance(images, dict):
            data["images"] = images

        return data

    @field_validator("manager", "deployment")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        if "/" in value or "\x00" in value or any(ord(ch) < 32 for ch in value):
            raise ValueError("contains an invalid character")
        return value

    @field_validator("images")
    @classmethod
    def validate_images(
        cls, value: dict[str, str | None] | None
    ) -> dict[str, str | None] | None:
        if value is None:
            return None

        cleaned: dict[str, str | None] = {}
        for container, image in value.items():
            if not CONTAINER_PATTERN.fullmatch(container):
                raise ValueError(
                    f"invalid container name {container!r}; use 1-253 "
                    "letters, digits, '.', '_' or '-'"
                )
            if image is not None:
                image = image.strip()
                if not image:
                    raise ValueError(
                        f"image for container {container!r} must not be empty"
                    )
                if len(image) > 1024:
                    raise ValueError(
                        f"image for container {container!r} is too long"
                    )
            cleaned[container] = image
        return cleaned

    def declared_values(self) -> dict[str, Any]:
        """Return only explicitly supplied, non-null fields as flat keys."""
        supplied = self.model_dump(exclude_unset=True)
        declared: dict[str, Any] = {}

        if "replicas" in supplied and self.replicas is not None:
            declared["replicas"] = self.replicas

        if "images" in supplied and self.images is not None:
            for container, image in self.images.items():
                if image is not None:
                    declared[image_field(container)] = image

        return declared


class StateResponse(BaseModel):
    deployment: str
    version: int
    config: dict[str, Any]
    merged_config: dict[str, Any]
    ownership: dict[str, Any]


class ChangeSet(BaseModel):
    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    co_managed: list[str] = Field(default_factory=list)
    taken_over: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    released: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)


class ClaimResponse(StateResponse):
    accepted: Literal[True] = True
    manager: str
    takeover: bool
    message: str
    changes: ChangeSet


class ConflictItem(BaseModel):
    field: str
    submitted: Any
    current: Any
    managers: list[str]
    other_managers: list[str]


class ConflictResponse(BaseModel):
    accepted: Literal[False] = False
    code: str
    message: str
    conflicts: list[ConflictItem]
    detail: dict[str, Any]
    deployment: str
    version: int
    config: dict[str, Any]
    merged_config: dict[str, Any]
    ownership: dict[str, Any]
