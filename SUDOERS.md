# Configuração de sudoers nos hosts gerenciados

Este é o **último controle de segurança real** entre o SAI e os seus servidores.

O motor de aprovação impede que a IA execute algo sem autorização — mas ele roda
dentro da aplicação. Se alguém comprometer a aplicação, ou se houver um bug que
eu não previ, o que sobra é a permissão da conta SSH no host. Por isso ela
**nunca** pode ter `sudo` irrestrito.

A regra: a conta de automação só pode rodar a lista exata de comandos que os
conectores realmente usam. Nada além disso.

---

## Comandos que o SAI executa

Levantados diretamente do código (`sai/connectors/linux_connector.py` e
`nginx_connector.py`):

| Comando | Precisa de sudo? | Usado por |
|---|---|---|
| `df -h`, `free -m`, `uptime`, `ps` | Não | `linux_get_system_metrics`, `linux_get_top_processes` |
| `tail -n N <arquivo>` | Depende do arquivo | `linux_tail_log` |
| `systemctl status <serviço> --no-pager` | Não | `linux_check_service_status` |
| `systemctl restart <serviço>` | **Sim** | `linux_restart_service` |
| `nginx -T` | **Sim** | `nginx_get_active_config` |
| `nginx -t` | **Sim** | `nginx_reload` (validação prévia) |
| `systemctl reload nginx` | **Sim** | `nginx_reload` |
| `tail -n 200 /var/log/nginx/*.log` | **Sim** | `nginx_tail_access_log`, `nginx_tail_error_log` |

---

## Arquivo de sudoers

Crie `/etc/sudoers.d/sai-automation` em cada host gerenciado:

```
# SAI — automação de infraestrutura.
# Conta sem senha, restrita a uma lista explícita de comandos.
# NUNCA conceda ALL=(ALL) NOPASSWD: ALL a esta conta.

Cmnd_Alias SAI_NGINX = /usr/sbin/nginx -t, \
                       /usr/sbin/nginx -T, \
                       /bin/systemctl reload nginx, \
                       /usr/bin/tail -n 200 /var/log/nginx/access.log, \
                       /usr/bin/tail -n 200 /var/log/nginx/error.log

# Liste UM POR UM os serviços que podem ser reiniciados neste host.
# Um curinga aqui (systemctl restart *) anula o propósito deste arquivo.
Cmnd_Alias SAI_SERVICES = /bin/systemctl restart nginx, \
                          /bin/systemctl restart php-fpm

sai-automation ALL=(root) NOPASSWD: SAI_NGINX, SAI_SERVICES

# Impede escalonamento via shell a partir dos comandos acima.
Defaults:sai-automation !authenticate
Defaults:sai-automation noexec
```

Valide antes de sair da sessão — um sudoers quebrado tranca todo mundo fora:

```bash
sudo visudo -c -f /etc/sudoers.d/sai-automation
```

Permissões corretas do arquivo:

```bash
sudo chmod 0440 /etc/sudoers.d/sai-automation
```

---

## Pontos que costumam passar batido

**Confira o caminho dos binários.** `systemctl` fica em `/bin/systemctl` no
Debian/Ubuntu e em `/usr/bin/systemctl` no RHEL/Rocky. Um caminho errado faz a
regra simplesmente não casar, e o comando falha em produção. Verifique com
`which systemctl` e `which nginx` em cada distribuição que você usa.

**Nada de curingas em `systemctl restart`.** `systemctl restart *` permite
reiniciar qualquer coisa, inclusive `sshd` — o que derrubaria seu próprio
acesso. Liste os serviços um a um.

**`tail` com caminho fixo.** Se você permitir `/usr/bin/tail` sem argumentos
fixos, a conta lê qualquer arquivo do sistema, incluindo `/etc/shadow`. Os
caminhos de log precisam estar explícitos na regra.

**A allowlist do SAI é independente disto.** O `allowlist.yaml` decide o que a
IA pode executar *sem aprovação humana*; o sudoers decide o que a conta pode
executar *no sistema operacional*. Os dois precisam estar restritos — um não
substitui o outro.

---

## Chave SSH

```bash
# Na sua estação, gere um par dedicado (não reaproveite chave pessoal):
ssh-keygen -t ed25519 -f ~/.ssh/sai_automation -C "sai-automation" -N ""

# Em cada host:
sudo useradd -m -s /bin/bash sai-automation
sudo mkdir -p /home/sai-automation/.ssh
sudo chmod 700 /home/sai-automation/.ssh
# copie o conteúdo de sai_automation.pub para:
sudo vi /home/sai-automation/.ssh/authorized_keys
sudo chmod 600 /home/sai-automation/.ssh/authorized_keys
sudo chown -R sai-automation:sai-automation /home/sai-automation/.ssh
```

Restrinja a chave em `authorized_keys` para reduzir o estrago em caso de
vazamento — `from=` limita a origem, e as opções desligam recursos que a
automação não usa:

```
from="10.0.0.15",no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... sai-automation
```

> `no-pty` é seguro aqui: os conectores executam comandos diretos, não sessões
> interativas. Se algum dia um comando precisar de TTY, remova apenas essa
> opção — não a linha inteira.

A chave privada vai para o Azure Key Vault e é montada no runtime no caminho
apontado por `LINUX_SSH_PRIVATE_KEY_PATH`. Nunca a coloque no repositório.

---

## Verificação

Depois de configurar, confirme que o cerco está fechado:

```bash
# Deve funcionar:
sudo -u sai-automation sudo -n systemctl restart nginx
sudo -u sai-automation sudo -n nginx -t

# DEVE FALHAR — se algum destes funcionar, o sudoers está permissivo demais:
sudo -u sai-automation sudo -n systemctl restart sshd
sudo -u sai-automation sudo -n cat /etc/shadow
sudo -u sai-automation sudo -n su -
sudo -u sai-automation sudo -n bash
```
