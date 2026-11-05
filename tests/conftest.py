import pytest

from apps.core.roles import assign_role
from apps.ingestion.parsers import PARSERS, register
from apps.ingestion.parsers.base import SiteParser, inside

if "example" not in PARSERS:

    @register
    class ExampleSite(SiteParser):
        """Testovací weby *.example: článek v <article>, tělo případně v articleBody."""

        key = "example"
        name = "Testovací web"
        hosts = ("example",)
        blocks = inside("//article")
        origin = '//*[@class="source-link"]/@data-source-url'
        listing_pattern = r"^/a/\d+$"


@pytest.fixture
def make_user(django_user_model):
    def make(username, role=None, password="pass-12345"):
        user = django_user_model.objects.create_user(username=username, password=password)
        if role:
            assign_role(user, role)
        return user

    return make


@pytest.fixture
def researcher(make_user):
    return make_user("researcher", "researcher")


@pytest.fixture
def observer(make_user):
    return make_user("observer", "observer")


@pytest.fixture
def researcher_client(client, researcher):
    client.force_login(researcher)
    return client


@pytest.fixture
def observer_client(client, observer):
    client.force_login(observer)
    return client
