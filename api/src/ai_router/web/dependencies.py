"""FastAPI dependencies giving endpoints access to the application container."""

from typing import Annotated, cast

from fastapi import Depends, Request

from ai_router.container import AppContainer


def get_container(request: Request) -> AppContainer:
    """The container is created in the application's lifespan and stored on `app.state`."""
    return cast(AppContainer, request.app.state.container)


ContainerDep = Annotated[AppContainer, Depends(get_container)]
