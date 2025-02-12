from urllib.parse import urlencode
from uuid import uuid4

from django.urls import reverse

from sentry.models.project import Project
from sentry.testutils.cases import APITestCase, PermissionTestCase
from sentry.testutils.silo import region_silo_test
from sentry.testutils.skips import requires_snuba
from sentry.utils.signing import sign

pytestmark = [requires_snuba]


@region_silo_test
class AcceptTransferProjectPermissionTest(PermissionTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.create_project(teams=[self.team])
        self.path = reverse("sentry-api-0-accept-project-transfer")

    def test_team_admin_cannot_load(self):
        self.assert_team_admin_cannot_access(self.path)


@region_silo_test
class AcceptTransferProjectTest(APITestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.create_user(email="example@example.com", is_superuser=False)
        self.from_organization = self.create_organization(owner=self.owner)
        self.to_organization = self.create_organization(owner=self.owner)
        self.from_team = self.create_team(organization=self.from_organization)
        self.to_team = self.create_team(organization=self.to_organization)
        user = self.create_user("admin@example.com")
        self.member = self.create_member(
            organization=self.from_organization, user=user, role="admin", teams=[self.from_team]
        )
        self.project = self.create_project(name="proj", teams=[self.from_team])
        self.transaction_id = uuid4().hex
        self.path = reverse("sentry-api-0-accept-project-transfer")

    def test_requires_authentication(self):
        response = self.client.get(self.path)
        assert response.status_code == 403
        assert response.data == {"detail": "Authentication credentials were not provided."}

    def test_handle_incorrect_url_data(self):
        self.login_as(self.owner)
        url_data = sign(
            actor_id=self.member.id,
            # This is bad data
            from_organization_id=9999999,
            project_id=self.project.id,
            user_id=self.owner.id,
            transaction_id=self.transaction_id,
        )
        resp = self.client.get(self.path + "?" + urlencode({"data": url_data}))
        assert resp.status_code == 400
        assert resp.data["detail"] == "Project no longer exists"
        resp = self.client.get(self.path)
        assert resp.status_code == 404

    def test_returns_org_options_with_signed_link(self):
        self.login_as(self.owner)
        url_data = sign(
            actor_id=self.member.user_id,
            from_organization_id=self.from_organization.id,
            project_id=self.project.id,
            user_id=self.owner.id,
            transaction_id=self.transaction_id,
        )

        resp = self.client.get(self.path + "?" + urlencode({"data": url_data}))
        assert resp.status_code == 200
        assert resp.data["project"]["slug"] == self.project.slug
        assert resp.data["project"]["id"] == self.project.id
        assert len(resp.data["organizations"]) == 2
        org_slugs = {o["slug"] for o in resp.data["organizations"]}
        assert self.from_organization.slug in org_slugs
        assert self.to_organization.slug in org_slugs

    def test_returns_org_options_with_signed_link__owner_through_team(self):
        user = self.create_user("bar@example.com")
        owner_team_from = self.create_team(organization=self.from_organization, org_role="owner")
        owner_team_to = self.create_team(organization=self.to_organization, org_role="owner")
        from_member = self.create_member(
            organization=self.from_organization,
            user=user,
            teams=[owner_team_from, self.from_team],
            role="member",
        )
        self.create_member(
            organization=self.to_organization,
            user=user,
            teams=[owner_team_to],
            role="member",
        )

        self.login_as(user)
        url_data = sign(
            actor_id=from_member.user_id,
            from_organization_id=self.from_organization.id,
            project_id=self.project.id,
            user_id=user.id,
            transaction_id=self.transaction_id,
        )

        resp = self.client.get(self.path + "?" + urlencode({"data": url_data}))
        assert resp.status_code == 200
        assert resp.data["project"]["slug"] == self.project.slug
        assert resp.data["project"]["id"] == self.project.id
        assert len(resp.data["organizations"]) == 2
        org_slugs = {o["slug"] for o in resp.data["organizations"]}
        assert self.from_organization.slug in org_slugs
        assert self.to_organization.slug in org_slugs

    def test_transfers_project_to_team_deprecated(self):
        self.login_as(self.owner)
        url_data = sign(
            actor_id=self.member.user_id,
            from_organization_id=self.from_organization.id,
            project_id=self.project.id,
            user_id=self.owner.id,
            transaction_id=self.transaction_id,
        )

        resp = self.client.post(
            self.path, data={"team": self.to_team.id, "organization": None, "data": url_data}
        )
        assert resp.status_code == 400
        assert resp.data == {"detail": "Cannot transfer projects to a team."}

    def test_non_owner_cannot_transfer_project(self):
        rando_user = self.create_user(email="blipp@bloop.com", is_superuser=False)
        rando_org = self.create_organization(name="supreme beans")

        self.login_as(rando_user)
        url_data = sign(
            actor_id=self.member.user_id,
            from_organization_id=rando_org.id,
            project_id=self.project.id,
            user_id=rando_user.id,
            transaction_id=self.transaction_id,
        )

        resp = self.client.post(
            self.path, data={"organization": self.to_organization.slug, "data": url_data}
        )
        assert resp.status_code == 400
        p = Project.objects.get(id=self.project.id)
        assert p.organization_id == self.from_organization.id
        assert p.organization_id != rando_org.id

    def test_non_owner_on_owner_team_can_transfer_project(self):
        rando_user = self.create_user(email="blipp@bloop.com", is_superuser=False)
        rando_org = self.create_organization(name="supreme beans")
        owner_team = self.create_team(organization=rando_org, org_role="owner")
        self.create_member(
            organization=rando_org, user=rando_user, teams=[owner_team], role="member"
        )

        self.login_as(rando_user)
        url_data = sign(
            actor_id=self.member.user_id,
            from_organization_id=rando_org.id,
            project_id=self.project.id,
            user_id=rando_user.id,
            transaction_id=self.transaction_id,
        )

        resp = self.client.post(
            self.path, data={"organization": self.to_organization.slug, "data": url_data}
        )
        assert resp.status_code == 400
        p = Project.objects.get(id=self.project.id)
        assert p.organization_id == self.from_organization.id
        assert p.organization_id != rando_org.id

    def test_transfers_project_to_correct_organization(self):
        self.login_as(self.owner)
        url_data = sign(
            actor_id=self.member.user_id,
            from_organization_id=self.from_organization.id,
            project_id=self.project.id,
            user_id=self.owner.id,
            transaction_id=self.transaction_id,
        )

        resp = self.client.post(
            self.path, data={"organization": self.to_organization.slug, "data": url_data}
        )
        assert resp.status_code == 204
        p = Project.objects.get(id=self.project.id)
        assert p.organization_id == self.to_organization.id

    def test_use_org_when_team_and_org_provided(self):
        self.login_as(self.owner)
        url_data = sign(
            actor_id=self.member.user_id,
            from_organization_id=self.from_organization.id,
            project_id=self.project.id,
            user_id=self.owner.id,
            transaction_id=self.transaction_id,
        )

        resp = self.client.post(
            self.path,
            data={
                "organization": self.to_organization.slug,
                "team": self.to_team.id,
                "data": url_data,
            },
        )
        assert resp.status_code == 204
        p = Project.objects.get(id=self.project.id)
        assert p.organization_id == self.to_organization.id
