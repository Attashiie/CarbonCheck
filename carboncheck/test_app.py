import os
import tempfile
import unittest

_database = tempfile.TemporaryDirectory()
os.environ["APP_ENV"] = "test"
os.environ["CARBONCHECK_DB_BACKEND"] = "sqlite"
os.environ["CARBONCHECK_DB"] = os.path.join(_database.name, "test.sqlite3")

from app import app, formatar_emissao


class CarbonCheckApiTests(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def test_emissions_use_readable_metric_units(self):
        self.assertEqual(formatar_emissao(0), "0 kgCO2e")
        self.assertEqual(formatar_emissao(0.0025), "2,5 kgCO2e")
        self.assertEqual(formatar_emissao(1), "1 tCO2e")
        self.assertEqual(formatar_emissao(1234.5), "1.234,5 tCO2e")

    def register(self, email="empresa@carboncheck.com.br", **extra):
        payload = {
            "nome": "Empresa Teste",
            "email": email,
            "senha": "Senha-segura-123",
            "perfil": "organizacao",
            **extra,
        }
        return self.client.post("/api/auth/cadastro", json=payload)

    def test_organization_registration_is_active_immediately(self):
        response = self.register(email="ativa@carboncheck.com.br")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["usuario"]["perfil"], "organizacao")

    def test_privileged_registration_requires_admin_approval(self):
        response = self.register(
            email="solicitante-verificador@carboncheck.com.br", perfil="verificador"
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json["pendente"])

        login_data = {
            "email": "solicitante-verificador@carboncheck.com.br",
            "senha": "Senha-segura-123",
        }
        self.assertEqual(
            self.client.post("/api/auth/login", json=login_data).status_code,
            403,
        )

        pending = app.test_cli_runner().invoke(args=["pending-users"])
        self.assertEqual(pending.exit_code, 0, pending.output)
        self.assertIn("solicitante-verificador@carboncheck.com.br", pending.output)

        admin_login = self.client.post(
            "/api/auth/login",
            json={"email": "admin@carboncheck.com.br", "senha": "demo1234"},
        )
        admin_headers = {
            "Authorization": f"Bearer {admin_login.json['token']}"
        }
        requests = self.client.get(
            "/api/admin/solicitacoes", headers=admin_headers
        )
        self.assertEqual(requests.status_code, 200)
        users = self.client.get("/api/admin/usuarios", headers=admin_headers)
        pending_user = next(
            user for user in users.json
            if user["email"] == "solicitante-verificador@carboncheck.com.br"
        )
        self.assertEqual(pending_user["perfil"], "verificador")
        self.assertEqual(pending_user["status"], "pendente_aprovacao")
        request_id = next(
            user["id"] for user in requests.json
            if user["email"] == "solicitante-verificador@carboncheck.com.br"
        )
        approved = self.client.post(
            f"/api/admin/solicitacoes/{request_id}/aprovar",
            headers=admin_headers,
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json["perfil"], "verificador")
        users = self.client.get("/api/admin/usuarios", headers=admin_headers)
        reviewed = next(
            user for user in users.json
            if user["email"] == "solicitante-verificador@carboncheck.com.br"
        )
        self.assertEqual(reviewed["perfil"], "verificador")
        self.assertEqual(reviewed["status"], "ativo")

        login = self.client.post("/api/auth/login", json=login_data)
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json["usuario"]["perfil"], "verificador")

    def test_non_admin_cannot_review_signup_requests(self):
        self.register(email="aguardando@carboncheck.com.br", perfil="regulador")
        organization = self.register(email="org2@carboncheck.com.br")
        headers = {"Authorization": f"Bearer {organization.json['token']}"}

        response = self.client.get("/api/admin/solicitacoes", headers=headers)

        self.assertEqual(response.status_code, 403)

    def test_admin_can_deactivate_and_reactivate_accounts(self):
        organization = self.register(email="managed@carboncheck.com.br")
        uid = organization.json["usuario"]["id"]
        admin = self.client.post(
            "/api/auth/login",
            json={"email": "admin@carboncheck.com.br", "senha": "demo1234"},
        )
        admin_headers = {"Authorization": f"Bearer {admin.json['token']}"}

        users = self.client.get("/api/admin/usuarios", headers=admin_headers)
        account = next(user for user in users.json if user["id"] == uid)
        self.assertEqual(account["status"], "ativo")
        self.assertEqual(account["perfil"], "organizacao")

        deactivated = self.client.post(
            f"/api/admin/usuarios/{uid}/status",
            headers=admin_headers,
            json={"ativo": False},
        )
        self.assertEqual(deactivated.status_code, 200)
        self.assertEqual(deactivated.json["status"], "desativado")
        self.assertEqual(
            self.client.get(
                "/api/inventarios",
                headers={"Authorization": f"Bearer {organization.json['token']}"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                json={"email": "managed@carboncheck.com.br", "senha": "Senha-segura-123"},
            ).status_code,
            403,
        )

        reactivated = self.client.post(
            f"/api/admin/usuarios/{uid}/status",
            headers=admin_headers,
            json={"ativo": True},
        )
        self.assertEqual(reactivated.status_code, 200)
        self.assertEqual(reactivated.json["status"], "ativo")

    def test_admin_cannot_disable_own_or_last_admin_account(self):
        admin = self.client.post(
            "/api/auth/login",
            json={"email": "admin@carboncheck.com.br", "senha": "demo1234"},
        )
        uid = admin.json["usuario"]["id"]
        headers = {"Authorization": f"Bearer {admin.json['token']}"}

        response = self.client.post(
            f"/api/admin/usuarios/{uid}/status",
            headers=headers,
            json={"ativo": False},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            self.client.get("/api/admin/usuarios", headers=headers).status_code,
            200,
        )

    def test_admin_deletes_account_and_owned_inventory_permanently(self):
        organization = self.register(email="to-delete@carboncheck.com.br")
        org_headers = {
            "Authorization": f"Bearer {organization.json['token']}"
        }
        created = self.client.post(
            "/api/inventarios", headers=org_headers, json={"periodo": 2024}
        )
        inventory_id = created.json["id"]

        admin = self.client.post(
            "/api/auth/login",
            json={"email": "admin@carboncheck.com.br", "senha": "demo1234"},
        )
        admin_headers = {"Authorization": f"Bearer {admin.json['token']}"}
        response = self.client.delete(
            f"/api/admin/usuarios/{organization.json['usuario']['id']}",
            headers=admin_headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["inventarios_removidos"], 1)
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                json={"email": "to-delete@carboncheck.com.br", "senha": "Senha-segura-123"},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/api/inventarios", headers=org_headers).status_code,
            401,
        )
        with app.app_context():
            from app import inventario_por_id

            self.assertIsNone(inventario_por_id(inventory_id))

    def test_deleting_verifier_removes_identity_from_verification_history(self):
        organization = self.register(email="history-owner@carboncheck.com.br")
        org_headers = {
            "Authorization": f"Bearer {organization.json['token']}"
        }
        created = self.client.post(
            "/api/inventarios", headers=org_headers, json={"periodo": 2025}
        )
        inventory_id = created.json["id"]
        self.client.post(
            f"/api/inventarios/{inventory_id}/lancamentos",
            headers=org_headers,
            json={"fonte": "gasolina", "quantidade": 100, "mes": 1},
        )
        self.client.post(
            f"/api/inventarios/{inventory_id}/submeter", headers=org_headers
        )

        self.register(
            email="verifier-to-delete@carboncheck.com.br", perfil="verificador"
        )
        admin = self.client.post(
            "/api/auth/login",
            json={"email": "admin@carboncheck.com.br", "senha": "demo1234"},
        )
        admin_headers = {"Authorization": f"Bearer {admin.json['token']}"}
        requests = self.client.get(
            "/api/admin/solicitacoes", headers=admin_headers
        )
        verifier_id = next(
            user["id"] for user in requests.json
            if user["email"] == "verifier-to-delete@carboncheck.com.br"
        )
        self.client.post(
            f"/api/admin/solicitacoes/{verifier_id}/aprovar",
            headers=admin_headers,
        )
        verifier = self.client.post(
            "/api/auth/login",
            json={
                "email": "verifier-to-delete@carboncheck.com.br",
                "senha": "Senha-segura-123",
            },
        )
        verifier_headers = {
            "Authorization": f"Bearer {verifier.json['token']}"
        }
        self.client.post(
            f"/api/inventarios/{inventory_id}/verificacao",
            headers=verifier_headers,
            json={"decisao": "aprovar", "parecer": "Verificação realizada."},
        )

        response = self.client.delete(
            f"/api/admin/usuarios/{verifier_id}",
            headers=admin_headers,
        )

        self.assertEqual(response.status_code, 200)
        detail = self.client.get(
            f"/api/inventarios/{inventory_id}", headers=org_headers
        )
        verification = detail.json["verificacoes"][0]
        self.assertNotIn("verificador", verification)
        self.assertNotIn("verificador_id", verification)
        self.assertEqual(verification["decisao"], "aprovado")

    def test_admin_cannot_delete_self_or_last_admin(self):
        admin = self.client.post(
            "/api/auth/login",
            json={"email": "admin@carboncheck.com.br", "senha": "demo1234"},
        )
        headers = {"Authorization": f"Bearer {admin.json['token']}"}
        uid = admin.json["usuario"]["id"]

        response = self.client.delete(
            f"/api/admin/usuarios/{uid}", headers=headers
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            self.client.get("/api/admin/usuarios", headers=headers).status_code,
            200,
        )

    def test_registration_rejects_unknown_profile(self):
        response = self.register(email="unknown@carboncheck.com.br", perfil="admin")

        self.assertEqual(response.status_code, 400)

    def test_registration_rejects_email_outside_carboncheck_domain(self):
        response = self.register(email="empresa@example.com")

        self.assertEqual(response.status_code, 400)
        self.assertIn("@carboncheck.com.br", response.json["erro"])

    def test_login_rejects_email_outside_carboncheck_domain(self):
        response = self.client.post(
            "/api/auth/login",
            json={"email": "admin@demo.com", "senha": "demo1234"},
        )

        self.assertEqual(response.status_code, 400)

    def test_registration_normalizes_domain_case(self):
        response = self.register(email="case@CARBONCHECK.COM.BR")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["usuario"]["email"], "case@carboncheck.com.br")

    def test_password_requires_eight_characters(self):
        response = self.client.post(
            "/api/auth/cadastro",
            json={"nome": "Empresa", "email": "curta@carboncheck.com.br", "senha": "Abc!123"},
        )

        self.assertEqual(response.status_code, 400)

    def test_password_requires_uppercase_lowercase_and_special_character(self):
        for password in ("abcdefg!", "abcdefgh!", "ABCDEFGH!", "Abcdefgh1"):
            with self.subTest(password=password):
                response = self.client.post(
                    "/api/auth/cadastro",
                    json={
                        "nome": "Empresa",
                        "email": f"{password}@carboncheck.com.br",
                        "senha": password,
                        "perfil": "organizacao",
                    },
                )
                self.assertEqual(response.status_code, 400)

        valid = self.client.post(
            "/api/auth/cadastro",
            json={
                "nome": "Empresa",
                "email": "forte@carboncheck.com.br",
                "senha": "Senha-forte1!",
                "perfil": "organizacao",
            },
        )
        self.assertEqual(valid.status_code, 201)

    def test_health_endpoint_checks_database(self):
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"status": "ok"})

    def test_inventory_and_user_are_available_from_persistent_storage(self):
        registration = self.register()
        token = registration.json["token"]
        headers = {"Authorization": f"Bearer {token}"}

        created = self.client.post(
            "/api/inventarios", headers=headers, json={"periodo": 2026}
        )
        self.assertEqual(created.status_code, 201)

        logged_in = self.client.post(
            "/api/auth/login",
            json={"email": "empresa@carboncheck.com.br", "senha": "Senha-segura-123"},
        )
        self.assertEqual(logged_in.status_code, 200)
        listed = self.client.get(
            "/api/inventarios",
            headers={"Authorization": f"Bearer {logged_in.json['token']}"},
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json), 1)
        self.assertEqual(listed.json[0]["periodo"], 2026)

    def test_transport_operation_indicators_feed_privileged_dashboard(self):
        registration = self.register(
            email="transportadora-indicadores@carboncheck.com.br",
            nome="Transportadora Indicadores Ltda",
        )
        org_headers = {"Authorization": f"Bearer {registration.json['token']}"}
        created = self.client.post(
            "/api/inventarios", headers=org_headers, json={"periodo": 2042}
        )
        inventory_id = created.json["id"]
        fuel = self.client.post(
            f"/api/inventarios/{inventory_id}/lancamentos",
            headers=org_headers,
            json={"fonte": "diesel", "mes": 1, "quantidade": 1000, "transporte": True},
        )
        self.assertEqual(fuel.status_code, 201)
        operation = {
            "mes": 1, "modal": "rodoviario", "tipo_veiculo": "Cavalo mecânico + carreta",
            "km_total": 10000, "km_vazio": 1200, "carga_t": 8, "tonelada_km": "",
            "viagens": 30, "veiculos": 5, "combustivel_l": 1000,
            "energia_kwh": 0, "refrigerante_kg": 0.2, "refrigeracao_h": 120,
            "frete_terceirizado_tkm": 500,
        }
        invalid = self.client.post(
            f"/api/inventarios/{inventory_id}/operacoes",
            headers=org_headers,
            json={**operation, "km_vazio": 11000},
        )
        self.assertEqual(invalid.status_code, 400)
        saved = self.client.post(
            f"/api/inventarios/{inventory_id}/operacoes",
            headers=org_headers,
            json=operation,
        )
        self.assertEqual(saved.status_code, 201)
        indicators = saved.json["indicadores_transporte"]
        self.assertEqual(indicators["km_total"], 10000)
        self.assertEqual(indicators["percentual_km_vazio"], 12)
        self.assertEqual(indicators["tonelada_km"], 80000)
        self.assertEqual(indicators["viagens"], 30)

        self.assertEqual(
            self.client.post(
                f"/api/inventarios/{inventory_id}/submeter", headers=org_headers
            ).status_code,
            200,
        )
        report = self.client.get(
            f"/api/inventarios/{inventory_id}/relatorio", headers=org_headers
        )
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json["indicadores_transporte"]["tonelada_km"], 80000)
        pdf = self.client.get(
            f"/api/inventarios/{inventory_id}/relatorio?formato=pdf",
            headers=org_headers,
        )
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertEqual(
            self.client.get("/api/painel/transporte", headers=org_headers).status_code,
            403,
        )
        regulator = self.client.post(
            "/api/auth/login",
            json={"email": "regulador@carboncheck.com.br", "senha": "demo1234"},
        )
        dashboard = self.client.get(
            "/api/painel/transporte",
            headers={"Authorization": f"Bearer {regulator.json['token']}"},
        )
        self.assertEqual(dashboard.status_code, 200)
        dashboard_headers = {"Authorization": "Bearer " + regulator.json["token"]}
        unapproved_year = self.client.get(
            "/api/painel/transporte?ano=2042", headers=dashboard_headers
        )
        self.assertEqual(unapproved_year.status_code, 200)
        self.assertEqual(unapproved_year.json["totais"]["inventarios"], 0)
        self.assertEqual(unapproved_year.json["totais"]["emissoes_tco2e"], 0)
        self.assertNotIn(
            2042,
            [item["periodo"] for item in unapproved_year.json["por_ano"]],
        )
        self.assertEqual(unapproved_year.json["por_categoria"], [])
        self.assertEqual(unapproved_year.json["por_transportadora"], [])
        self.assertEqual(unapproved_year.json["por_status"]["submetido"], 1)

        verifier = self.client.post(
            "/api/auth/login",
            json={"email": "verificador@carboncheck.com.br", "senha": "demo1234"},
        )
        verified = self.client.post(
            f"/api/inventarios/{inventory_id}/verificacao",
            headers={"Authorization": "Bearer " + verifier.json["token"]},
            json={"decisao": "aprovar", "parecer": "Inventário conferido."},
        )
        self.assertEqual(verified.status_code, 200)
        dashboard = self.client.get(
            "/api/painel/transporte?ano=2042", headers=dashboard_headers
        )
        self.assertEqual(dashboard.status_code, 200)
        company = next(
            item for item in dashboard.json["por_transportadora"]
            if item["organizacao"] == "Transportadora Indicadores Ltda"
        )
        self.assertEqual(company["emissao_transporte_tco2e"], 2.6)
        self.assertEqual(company["intensidade_tco2e_tkm"], 0.0000325)
        self.assertEqual(dashboard.json["totais"]["percentual_km_vazio"], 12)
        self.assertEqual(dashboard.json["totais"]["intensidade_tco2e_tkm"], 0.0000325)
        self.assertEqual(dashboard.json["por_modal"]["rodoviario"]["viagens"], 30)
        self.assertEqual(dashboard.json["anos_disponiveis"], [2025, 2042])
        selected_year = self.client.get(
            "/api/painel/transporte?ano=2042", headers=dashboard_headers
        )
        self.assertEqual(selected_year.status_code, 200)
        self.assertEqual(selected_year.json["ano_selecionado"], 2042)
        self.assertEqual(selected_year.json["totais"]["emissoes_tco2e"], 2.6)
        self.assertEqual(
            selected_year.json["por_categoria"][0],
            {"categoria": "Cat. 1 - Emissões diretas", "emissoes_tco2e": 2.6},
        )
        invalid_year = self.client.get(
            "/api/painel/transporte?ano=ano", headers=dashboard_headers
        )
        self.assertEqual(invalid_year.status_code, 400)


if __name__ == "__main__":
    unittest.main()
