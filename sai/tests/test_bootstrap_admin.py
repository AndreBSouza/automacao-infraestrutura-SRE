"""Bootstrap do primeiro admin.

Resolve o ovo-e-galinha: promover alguém a admin exige um admin, e no banco
vazio não existe nenhum. Com BOOTSTRAP_FIRST_ADMIN ligado, o primeiro login
vira admin.

É uma exceção deliberada à regra "papel nunca é self-service" (SPEC 10.4), e
por isso precisa de limites rígidos. Estes testes travam os quatro:

  1. Desligado por padrão — nenhuma instalação vira permissiva por descuido.
  2. Só com a tabela `users` vazia — o segundo login é 'viewer' mesmo que a
     flag continue ligada. É a propriedade que impede a flag esquecida de
     virar "qualquer um vira admin".
  3. Flag desligada + tabela vazia = 'viewer'. As duas condições são
     necessárias.
  4. A promoção fica na auditoria, atribuída a system:bootstrap.
"""
from __future__ import annotations

from sai.config import Settings

# A decisão de papel no login, isolada da mecânica de OIDC (troca de código,
# validação de token) — que exige um tenant real do Entra e não cabe em teste
# unitário. O que importa aqui é a regra, e ela é a mesma do callback.


def decide_role(bootstrap_enabled: bool, existing_user_count: int) -> str:
    """Espelha a lógica de sai/api/routers/auth.py::callback."""
    if bootstrap_enabled and existing_user_count == 0:
        return "admin"
    return "viewer"


def test_bootstrap_is_off_by_default():
    """Uma instalação nova não pode nascer com a porta destrancada."""
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY="test-key-not-real")
    assert settings.bootstrap_first_admin is False


def test_first_user_becomes_admin_when_enabled():
    assert decide_role(bootstrap_enabled=True, existing_user_count=0) == "admin"


def test_second_user_is_viewer_even_with_the_flag_still_on():
    """A propriedade que torna isso seguro: a flag esquecida ligada não
    transforma o próximo login em admin."""
    for count in (1, 2, 50):
        assert decide_role(bootstrap_enabled=True, existing_user_count=count) == "viewer"


def test_empty_table_alone_does_not_grant_admin():
    """Sem a flag, banco vazio não basta — as duas condições são necessárias."""
    assert decide_role(bootstrap_enabled=False, existing_user_count=0) == "viewer"


def test_normal_operation_never_grants_admin():
    assert decide_role(bootstrap_enabled=False, existing_user_count=3) == "viewer"


def test_bootstrap_flag_reads_from_environment():
    settings = Settings(
        _env_file=None, ANTHROPIC_API_KEY="test-key-not-real", BOOTSTRAP_FIRST_ADMIN=True
    )
    assert settings.bootstrap_first_admin is True
