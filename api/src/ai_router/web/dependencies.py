"""FastAPI dependencies: the application container and the authenticated client."""

from typing import Annotated, cast

from fastapi import Depends, Request

from ai_router.container import AppContainer
from ai_router.web.security import AuthContext


def get_container(request: Request) -> AppContainer:
    """The container is created in the application's lifespan and stored on `app.state`."""
    return cast(AppContainer, request.app.state.container)


ContainerDep = Annotated[AppContainer, Depends(get_container)]


async def get_auth(request: Request, container: ContainerDep) -> AuthContext:
    return await container.authenticator.authenticate(request)


AuthDep = Annotated[AuthContext, Depends(get_auth)]
