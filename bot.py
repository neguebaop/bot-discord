import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import asyncio
import random
import hashlib
import time
from flask import Flask
from threading import Thread

try:
    from supabase import create_client
except Exception:
    create_client = None

import os
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = 1347336329280753785
ARQUIVO = "filas.json"

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_TABLE = "filas"

supabase = None
if SUPABASE_URL and SUPABASE_KEY and create_client is not None:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("Supabase conectado com sucesso.")
    except Exception as e:
        print(f"Aviso: não foi possível conectar no Supabase: {e}")
        supabase = None


IMAGEM_URL = "https://cdn.discordapp.com/attachments/1192768001364201524/1472035211351953408/orglink.jpg?ex=69911b1f&is=698fc99f&hm=cf22b26862eb8a2a59ffcc7e2e22c31e8ad9854143bca65cc56de51e285ca830"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


def dados_padrao():
    return {
        "filas": {},
        "jogadores": {},
        "ranking": {},
        "derrotas": {},
        "loja": {},
        "blacklist": [],
        "coins": {},
        "fila_tempo": {},
        "perfis": {}
    }


def _salvar_local(dados):
    with open(ARQUIVO, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=4, ensure_ascii=False)


def _carregar_local():
    if not os.path.exists(ARQUIVO):
        dados = dados_padrao()
        _salvar_local(dados)
        return dados

    with open(ARQUIVO, "r", encoding="utf-8") as f:
        return json.load(f)


def _corrigir_dados(dados):
    if not isinstance(dados, dict):
        dados = dados_padrao()

    padrao = dados_padrao()
    for chave, valor in padrao.items():
        if chave not in dados:
            dados[chave] = valor
    return dados


def salvar(dados):
    dados = _corrigir_dados(dados)

    if supabase is None:
        _salvar_local(dados)
        return

    try:
        supabase.table(SUPABASE_TABLE).upsert({
            "id": 1,
            "user_id": "bot",
            "fila_nome": "principal",
            "dados": dados
        }, on_conflict="id").execute()
        _salvar_local(dados)  # backup local também
    except Exception as e:
        print(f"Erro ao salvar no Supabase, salvando localmente: {e}")
        _salvar_local(dados)


def carregar():
    if supabase is None:
        return _corrigir_dados(_carregar_local())

    try:
        resposta = supabase.table(SUPABASE_TABLE).select("dados").eq("id", 1).execute()

        if resposta.data and len(resposta.data) > 0:
            return _corrigir_dados(resposta.data[0].get("dados", {}))

        # Primeira vez usando Supabase: tenta migrar o filas.json local.
        if os.path.exists(ARQUIVO):
            dados = _corrigir_dados(_carregar_local())
        else:
            dados = dados_padrao()

        salvar(dados)
        return dados

    except Exception as e:
        print(f"Erro ao carregar do Supabase, usando arquivo local: {e}")
        return _corrigir_dados(_carregar_local())

def is_mediador(interaction: discord.Interaction):
    return (
        interaction.user.guild_permissions.administrator
        or interaction.user.id == interaction.guild.owner_id
        or any(role.name.lower() == "mediador" for role in interaction.user.roles)
    )


def is_streamer(interaction: discord.Interaction):
    return (
        interaction.user.guild_permissions.administrator
        or any(role.name.lower() == "streamer" for role in interaction.user.roles)
    )


def is_admin(interaction: discord.Interaction):
    return interaction.user.guild_permissions.administrator


class MeuBot(commands.Bot):
    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        print(f"Comandos sincronizados no servidor: {len(synced)}")
        for cmd in synced:
            print(f"/{cmd.name}")


bot = MeuBot(command_prefix="!", intents=intents)
tree = bot.tree


@bot.event
async def on_ready():
    if not getattr(bot, "views_persistentes_registradas", False):
        registrar_views_persistentes()
        bot.views_persistentes_registradas = True

    if not limpar_filas_presos.is_running():
        limpar_filas_presos.start()

    print(f"Bot online como {bot.user}")


# =========================
# BOTÕES PERSISTENTES
# =========================

def make_custom_id(prefix, nome):
    """Gera um custom_id fixo e curto para os botões continuarem funcionando após deploy/restart."""
    nome_hash = hashlib.sha256(nome.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{nome_hash}"


def registrar_views_persistentes():
    """Reconecta os botões das mensagens antigas usando as filas salvas no Supabase/JSON."""
    dados = carregar()
    total = 0

    for nome, fila in dados.get("filas", {}).items():
        try:
            if "streamer" in fila:
                bot.add_view(FilaStreamerView(nome))
            else:
                bot.add_view(FilaView(nome))
                compat = FilaCompatAntigaView(nome)
                if len(compat.children) > 0:
                    bot.add_view(compat)
            total += 1
        except Exception as e:
            print(f"Aviso: não foi possível registrar view da fila {nome}: {e}")

    try:
        bot.add_view(BlacklistView())
    except Exception:
        pass

    try:
        bot.add_view(PerfilView())
    except Exception:
        pass

    try:
        bot.add_view(LojaView())
    except Exception:
        pass

    print(f"Views persistentes registradas: {total}")

# =========================
# LIMPEZA AUTOMÁTICA DE FILAS
# =========================

TEMPO_MAXIMO_FILA = 20 * 60  # 20 minutos

def limpar_jogadores_antigos(dados: dict) -> int:
    """Remove jogadores presos há mais de 20 minutos somente das filas normais.
    Não mexe em ranking, coins, blacklist, loja/presentes ou perfil.
    """
    dados = _corrigir_dados(dados)
    agora = time.time()
    fila_tempo = dados.setdefault("fila_tempo", {})
    removidos = 0

    for nome, fila in list(dados.get("filas", {}).items()):
        # fila de streamer cria sala separada; não limpamos aqui para não remover fila de live ativa.
        if "streamer" in fila:
            continue

        jogadores = fila.get("jogadores", [])
        modo = fila.setdefault("modo", {})
        novos_jogadores = []

        for uid in jogadores:
            uid_str = str(uid)
            entrou_em = fila_tempo.get(nome, {}).get(uid_str)

            # Se veio de versão antiga sem horário, começa a contar agora.
            if entrou_em is None:
                fila_tempo.setdefault(nome, {})[uid_str] = agora
                novos_jogadores.append(uid)
                continue

            if agora - float(entrou_em) >= TEMPO_MAXIMO_FILA:
                removidos += 1
                modo.pop(uid_str, None)
                fila_tempo.get(nome, {}).pop(uid_str, None)
            else:
                novos_jogadores.append(uid)

        fila["jogadores"] = novos_jogadores

    return removidos


@tasks.loop(seconds=60)
async def limpar_filas_presos():
    try:
        dados = carregar()
        removidos = limpar_jogadores_antigos(dados)
        if removidos > 0:
            salvar(dados)
            print(f"Limpeza automática: {removidos} jogador(es) removido(s) de filas normais após 20 minutos.")
    except Exception as e:
        print(f"Erro na limpeza automática das filas: {e}")


@limpar_filas_presos.before_loop
async def antes_limpar_filas_presos():
    await bot.wait_until_ready()


# =========================
# VIEW FILA
# =========================

def tipo_fila(nome: str) -> str:
    nome_lower = nome.lower()

    # 1v1 / 1x1, inclusive Mobile e Emulador: Gel Normal + Gel Infinito + Sair
    if "1v1" in nome_lower or "1x1" in nome_lower:
        return "1v1_gel"

    if ("2v2" in nome_lower or "2x2" in nome_lower) and "misto" in nome_lower:
        return "2v2_misto"

    if ("3v3" in nome_lower or "3x3" in nome_lower) and "misto" in nome_lower:
        return "3v3_misto"

    if ("4v4" in nome_lower or "4x4" in nome_lower) and "misto" in nome_lower:
        return "4v4_misto"

    return "normal"


def texto_modo(nome: str, modo: int) -> str:
    tipo = tipo_fila(nome)

    if tipo == "1v1_gel":
        return "Gel Normal" if modo == 1 else "Gel Infinito"

    if tipo in ("2v2_misto", "3v3_misto", "4v4_misto"):
        return f"{modo} Emulador(es)"

    return ""


def extrair_valor_fila(nome: str) -> float:
    """Tenta recuperar o valor da fila pelo nome salvo na mensagem.
    Ex: '1v1 mobile - R$100.00' -> 100.0
    Isso evita que uma fila antiga fique inutilizável se ela sumir do Supabase por reset/bug anterior.
    """
    try:
        if "R$" in nome:
            parte = nome.split("R$", 1)[1].strip().split()[0]
            if "," in parte:
                parte = parte.replace(".", "").replace(",", ".")
            return float(parte)
    except Exception:
        pass
    return 0.0



def uid_str(user_id):
    return str(user_id)

def lista_tem_usuario(lista, user_id):
    return any(str(x) == str(user_id) for x in (lista or []))

def adicionar_usuario_lista(lista, user_id):
    if not lista_tem_usuario(lista, user_id):
        lista.append(uid_str(user_id))

def remover_usuario_lista(lista, user_id):
    removidos = 0
    for item in list(lista or []):
        if str(item) == str(user_id):
            lista.remove(item)
            removidos += 1
    return removidos

def guild_get_member_safe(guild, user_id):
    try:
        return guild.get_member(int(user_id))
    except Exception:
        return None

def garantir_fila_salva(dados: dict, nome: str) -> dict:
    """Garante que a fila exista no banco.

    Se a mensagem da fila ainda existe no Discord, mas o registro sumiu do Supabase
    por algum reset/deploy/versão anterior do /encerrar_fila, o bot recria a fila
    automaticamente em vez de responder 'Fila não encontrada'.
    """
    if "filas" not in dados or not isinstance(dados["filas"], dict):
        dados["filas"] = {}

    if nome not in dados["filas"]:
        dados["filas"][nome] = {
            "valor": extrair_valor_fila(nome),
            "max": 50,
            "jogadores": [],
            "modo": {}
        }
        salvar(dados)
        print(f"Fila recriada automaticamente no banco: {nome}")

    fila = dados["filas"][nome]
    if "jogadores" not in fila or not isinstance(fila["jogadores"], list):
        fila["jogadores"] = []
    if "modo" not in fila or not isinstance(fila["modo"], dict):
        fila["modo"] = {}
    if "valor" not in fila:
        fila["valor"] = extrair_valor_fila(nome)
    if "max" not in fila:
        fila["max"] = 50
    if "salas_criadas" not in fila or not isinstance(fila.get("salas_criadas"), list):
        fila["salas_criadas"] = []
    return fila


class FilaView(discord.ui.View):
    def __init__(self, nome):
        super().__init__(timeout=None)
        self.nome = nome

        tipo = tipo_fila(nome)

        # 1v1 / 1x1, inclusive Mobile e Emulador: Gel Normal + Gel Infinito + Sair
        if tipo == "1v1_gel":
            self.add_item(GelNormal(nome))
            self.add_item(GelInfinito(nome))
            self.add_item(Sair(nome))

        # 2v2 Misto: apenas 1 Emu + Sair
        elif tipo == "2v2_misto":
            self.add_item(Emu1(nome))
            self.add_item(Sair(nome))

        # 3v3 Misto: 1 Emu + 2 Emu + Sair
        elif tipo == "3v3_misto":
            self.add_item(Emu1(nome))
            self.add_item(Emu2(nome))
            self.add_item(Sair(nome))

        # 4v4 Misto: 1 Emu + 2 Emu + 3 Emu + Sair
        elif tipo == "4v4_misto":
            self.add_item(Emu1(nome))
            self.add_item(Emu2(nome))
            self.add_item(Emu3(nome))
            self.add_item(Sair(nome))

        else:
            self.add_item(Entrar(nome))
            self.add_item(Sair(nome))


class FilaStreamerView(discord.ui.View):
    def __init__(self, nome):
        super().__init__(timeout=None)
        self.nome = nome
        self.add_item(EntrarStreamer(nome))
        self.add_item(SairStreamer(nome))


class EntrarStreamer(discord.ui.Button):
    def __init__(self, nome):
        super().__init__(
            label="✅ Entrar na Fila",
            style=discord.ButtonStyle.success,
            custom_id=make_custom_id("entrar_streamer", nome)
        )
        self.nome = nome

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        dados = carregar()
        fila = garantir_fila_salva(dados, self.nome)

        if "streamer" not in fila:
            await interaction.followup.send("❌ Essa fila não é de streamer.", ephemeral=True)
            return

        if str(interaction.user.id) == str(fila.get("streamer")):
            await interaction.followup.send("❌ Você não pode entrar na sua própria fila.", ephemeral=True)
            return

        if "salas_criadas" not in fila or not isinstance(fila.get("salas_criadas"), list):
            fila["salas_criadas"] = []
        if "jogadores" not in fila or not isinstance(fila.get("jogadores"), list):
            fila["jogadores"] = []

        adicionar_usuario_lista(fila["jogadores"], interaction.user.id)
        salvar(dados)

        try:
            await atualizar_embed(interaction, self.nome)
        except Exception as e:
            print(f"Erro ao atualizar embed da fila streamer: {e}")

        streamer = guild_get_member_safe(interaction.guild, fila["streamer"])
        jogador = guild_get_member_safe(interaction.guild, interaction.user.id)

        if not streamer or not jogador:
            await interaction.followup.send(
                "❌ Entrei na fila, mas não achei o streamer ou jogador no servidor.",
                ephemeral=True
            )
            return

        # Se já criou sala para esse jogador, não cria duplicada.
        if lista_tem_usuario(fila.get("salas_criadas", []), interaction.user.id):
            await interaction.followup.send(
                "✅ Você já está na fila. Sua sala já foi criada.",
                ephemeral=True
            )
            return

        try:
            await criar_sala_privada(
                interaction.guild,
                [jogador, streamer],
                self.nome,
                1
            )

            dados = carregar()
            fila = garantir_fila_salva(dados, self.nome)
            if "salas_criadas" not in fila or not isinstance(fila.get("salas_criadas"), list):
                fila["salas_criadas"] = []
            adicionar_usuario_lista(fila["salas_criadas"], interaction.user.id)
            salvar(dados)

            await interaction.followup.send(
                "✅ Você entrou na fila e sua sala privada foi criada.",
                ephemeral=True
            )

        except Exception as e:
            print(f"Erro ao criar sala privada da fila streamer: {e}")
            await interaction.followup.send(
                "❌ Entrei na fila, mas não consegui criar a sala privada. Verifique se o bot tem Gerenciar Canais e se o cargo dele está acima dos outros.",
                ephemeral=True
            )

class SairStreamer(discord.ui.Button):
    def __init__(self, nome):
        super().__init__(
            label="❌ Sair da Fila",
            style=discord.ButtonStyle.danger,
            custom_id=make_custom_id("sair_streamer", nome)
        )
        self.nome = nome

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        dados = carregar()
        removido = False
        nome_para_atualizar = self.nome

        # Primeiro tenta pela fila do botão.
        fila = garantir_fila_salva(dados, self.nome)
        if "jogadores" not in fila or not isinstance(fila.get("jogadores"), list):
            fila["jogadores"] = []
        if "salas_criadas" not in fila or not isinstance(fila.get("salas_criadas"), list):
            fila["salas_criadas"] = []

        if lista_tem_usuario(fila["jogadores"], interaction.user.id) or lista_tem_usuario(fila["salas_criadas"], interaction.user.id):
            remover_usuario_lista(fila["jogadores"], interaction.user.id)
            remover_usuario_lista(fila["salas_criadas"], interaction.user.id)
            removido = True

        # Segurança: se por algum bug antigo ele estiver preso em outra fila streamer, remove também.
        for nome_fila, info in dados.get("filas", {}).items():
            if not isinstance(info, dict) or "streamer" not in info:
                continue
            if "jogadores" not in info or not isinstance(info.get("jogadores"), list):
                info["jogadores"] = []
            if "salas_criadas" not in info or not isinstance(info.get("salas_criadas"), list):
                info["salas_criadas"] = []
            if lista_tem_usuario(info["jogadores"], interaction.user.id) or lista_tem_usuario(info["salas_criadas"], interaction.user.id):
                remover_usuario_lista(info["jogadores"], interaction.user.id)
                remover_usuario_lista(info["salas_criadas"], interaction.user.id)
                nome_para_atualizar = nome_fila
                removido = True

        salvar(dados)

        try:
            await atualizar_embed(interaction, nome_para_atualizar)
        except Exception as e:
            print(f"Erro ao atualizar embed após sair: {e}")

        if removido:
            await interaction.followup.send("✅ Você saiu da fila.", ephemeral=True)
        else:
            # Não trava mais o usuário com mensagem falsa. O botão limpa mesmo assim.
            await interaction.followup.send("✅ Limpei sua fila. Se seu nome ainda aparecer, reinicie a mensagem criando a fila novamente.", ephemeral=True)


class Entrar(discord.ui.Button):
    def __init__(self, nome):
        super().__init__(
            label="Entrar na fila",
            style=discord.ButtonStyle.success,
            custom_id=make_custom_id("entrar", nome)
        )
        self.nome = nome

    async def callback(self, interaction: discord.Interaction):
        await entrar_fila(interaction, self.nome)


class GelNormal(discord.ui.Button):
    def __init__(self, nome):
        super().__init__(
            label="🧊 Gel Normal",
            style=discord.ButtonStyle.primary,
            custom_id=make_custom_id("gel_normal", nome)
        )
        self.nome = nome

    async def callback(self, interaction: discord.Interaction):
        await entrar_fila(interaction, self.nome, emuladores=1)


class GelInfinito(discord.ui.Button):
    def __init__(self, nome):
        super().__init__(
            label="🧊 Gel Infinito",
            style=discord.ButtonStyle.secondary,
            custom_id=make_custom_id("gel_infinito", nome)
        )
        self.nome = nome

    async def callback(self, interaction: discord.Interaction):
        await entrar_fila(interaction, self.nome, emuladores=2)


class Emu1(discord.ui.Button):
    def __init__(self, nome):
        super().__init__(
            label="💻 1 Emu",
            style=discord.ButtonStyle.primary,
            custom_id=make_custom_id("emu1", nome)
        )
        self.nome = nome

    async def callback(self, interaction: discord.Interaction):
        await entrar_fila(interaction, self.nome, emuladores=1)


class Emu2(discord.ui.Button):
    def __init__(self, nome):
        super().__init__(
            label="💻 2 Emu",
            style=discord.ButtonStyle.secondary,
            custom_id=make_custom_id("emu2", nome)
        )
        self.nome = nome

    async def callback(self, interaction: discord.Interaction):
        await entrar_fila(interaction, self.nome, emuladores=2)


class Emu3(discord.ui.Button):
    def __init__(self, nome):
        super().__init__(
            label="💻 3 Emu",
            style=discord.ButtonStyle.success,
            custom_id=make_custom_id("emu3", nome)
        )
        self.nome = nome

    async def callback(self, interaction: discord.Interaction):
        await entrar_fila(interaction, self.nome, emuladores=3)


class Sair(discord.ui.Button):
    def __init__(self, nome):
        super().__init__(
            label="❌ Sair da fila",
            style=discord.ButtonStyle.danger,
            custom_id=make_custom_id("sair", nome)
        )
        self.nome = nome

    async def callback(self, interaction: discord.Interaction):
        dados = carregar()
        # Não deixa mensagem antiga morrer se o registro sumiu do banco.
        fila = garantir_fila_salva(dados, self.nome)

        if not lista_tem_usuario(fila["jogadores"], interaction.user.id):
            await interaction.response.send_message("Você não está nessa fila.", ephemeral=True)
            return

        remover_usuario_lista(fila["jogadores"], interaction.user.id)
        if "modo" in fila and str(interaction.user.id) in fila["modo"]:
            del fila["modo"][str(interaction.user.id)]
        dados.setdefault("fila_tempo", {}).setdefault(self.nome, {}).pop(str(interaction.user.id), None)

        salvar(dados)
        await interaction.response.send_message("❌ Você saiu da fila.", ephemeral=True)
        await atualizar_embed(interaction, self.nome)


class FilaCompatAntigaView(discord.ui.View):
    """Registra botões de mensagens antigas para não falharem depois de deploy.

    Exemplo: antes algumas filas 1v1 Emulador/Mobile tinham o botão verde
    "Entrar na fila". Na versão nova elas usam "1 Emu". Sem esta view,
    essas mensagens antigas ficam com "Esta interação falhou" após reiniciar.
    """
    def __init__(self, nome):
        super().__init__(timeout=None)
        tipo = tipo_fila(nome)

        if tipo == "1v1_gel":
            self.add_item(Entrar(nome))

        elif tipo == "2v2_misto":
            self.add_item(Emu2(nome))

        elif tipo == "3v3_misto":
            self.add_item(Emu3(nome))



# =========================
# BLACKLIST
# =========================

class BlacklistView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(VerificarButton())


class VerificarButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="🔍 Verificar ID",
            style=discord.ButtonStyle.primary,
            custom_id="blacklist:verificar"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(VerificarModal())


class VerificarModal(discord.ui.Modal, title="Verificar ID na Blacklist"):
    id_input = discord.ui.TextInput(
        label="Digite o ID do Free Fire",
        placeholder="Ex: 123456789",
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        dados = carregar()
        id_digitado = self.id_input.value.strip()

        if id_digitado in dados.get("blacklist", []):
            embed = discord.Embed(
                title="🚫 ID Encontrado",
                description=f"O ID `{id_digitado}` está na blacklist.\n❌ Proibido de jogar na organização.",
                color=discord.Color.red()
            )
        else:
            embed = discord.Embed(
                title="✅ ID Liberado",
                description=f"O ID `{id_digitado}` NÃO está na blacklist.\n✔ Jogador liberado.",
                color=discord.Color.green()
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)


# =========================
# LOJA
# =========================

class LojaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(LojaButton())


class LojaButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Clique aqui para comprar algum item da loja...",
            style=discord.ButtonStyle.green,
            custom_id="loja:abrir"
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Escolha um item abaixo:",
            view=LojaSelectView(),
            ephemeral=True
        )


class LojaSelect(discord.ui.Select):
    def __init__(self):
        dados = carregar()
        opcoes = []

        for nome, info in dados["loja"].items():
            opcoes.append(
                discord.SelectOption(
                    label=nome,
                    description=f"{info['preco']} LinCoins"
                )
            )

        if not opcoes:
            opcoes.append(
                discord.SelectOption(
                    label="Loja vazia",
                    description="Nenhum item configurado"
                )
            )

        super().__init__(
            placeholder="Escolha um item...",
            options=opcoes,
            disabled=(opcoes[0].label == "Loja vazia")
        )

    async def callback(self, interaction: discord.Interaction):
        dados = carregar()
        user_id = str(interaction.user.id)

        item = self.values[0]
        preco = dados["loja"][item]["preco"]
        cargo_id = dados["loja"][item]["cargo_id"]

        saldo = dados["coins"].get(user_id, 0)

        if saldo < preco:
            await interaction.response.send_message(
                f"❌ Você não tem LinCoins suficientes.\nSaldo: {saldo}",
                ephemeral=True
            )
            return

        dados["coins"][user_id] = saldo - preco
        salvar(dados)

        cargo = interaction.guild.get_role(cargo_id)
        if cargo:
            await interaction.user.add_roles(cargo)

        await interaction.response.send_message(
            f"✅ Você comprou **{item}** por {preco} LinCoins!\n"
            f"💰 Saldo restante: {dados['coins'][user_id]}",
            ephemeral=True
        )


class LojaSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(LojaSelect())


# =========================
# PERFIL
# =========================

class PerfilView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Perfil", style=discord.ButtonStyle.primary, custom_id="perfil:ver")
    async def ver_perfil(self, interaction: discord.Interaction, button: discord.ui.Button):
        dados = carregar()
        user_id = str(interaction.user.id)

        vitorias = dados["ranking"].get(user_id, 0)
        derrotas = dados["derrotas"].get(user_id, 0)
        coins = dados["coins"].get(user_id, 0)

        partidas = vitorias + derrotas
        taxa = (vitorias / partidas * 100) if partidas > 0 else 0

        ranking_ordenado = sorted(
            dados["ranking"].items(),
            key=lambda x: x[1],
            reverse=True
        )

        posicao = 0
        medalha = ""

        for index, (uid, _) in enumerate(ranking_ordenado, start=1):
            if uid == user_id:
                posicao = index
                break

        if posicao == 1:
            medalha = "🥇"
        elif posicao == 2:
            medalha = "🥈"
        elif posicao == 3:
            medalha = "🥉"

        embed = discord.Embed(
            title=f"{medalha} Perfil de {interaction.user.display_name}",
            color=0x00ff99
        )

        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="🏆 Vitórias", value=vitorias, inline=True)
        embed.add_field(name="❌ Derrotas", value=derrotas, inline=True)
        embed.add_field(name="🎮 Partidas", value=partidas, inline=True)
        embed.add_field(name="💰 LinCoins", value=coins, inline=True)
        embed.add_field(name="📈 Taxa de Vitória", value=f"{taxa:.1f}%", inline=True)
        embed.add_field(name="🏅 Posição no Ranking", value=f"#{posicao}", inline=True)

        await interaction.response.send_message(
            embed=embed,
            view=PerfilLojaView(),
            ephemeral=True
        )


class PerfilLojaView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="🛒 Ir para Loja", style=discord.ButtonStyle.success, custom_id="perfil:loja")
    async def ir_loja(self, interaction: discord.Interaction, button: discord.ui.Button):
        dados = carregar()
        user_id = str(interaction.user.id)
        saldo = dados["coins"].get(user_id, 0)

        embed = discord.Embed(
            title="🛒 Loja do Servidor",
            description="Escolha um item abaixo para comprar.",
            color=0x2b2d31
        )

        embed.add_field(name="💰 Seu saldo", value=saldo, inline=False)

        await interaction.response.send_message(
            embed=embed,
            view=LojaView(),
            ephemeral=True
        )


# =========================
# SALA PRIVADA
# =========================

async def criar_sala_privada(guild, membros, nome_fila, emuladores):
    nome_categoria = "SUA FILA AQUI"
    categoria = discord.utils.get(guild.categories, name=nome_categoria)

    if not categoria:
        categoria = await guild.create_category(nome_categoria)

    nome_limpo = nome_fila.strip()
    numero_partida = len(categoria.channels) + 1
    nome_canal = f"partida-{nome_limpo.lower().replace(' ', '-')}-{numero_partida}"

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False)
    }

    for membro in membros:
        if membro is not None:
            overwrites[membro] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )

    canal = await guild.create_text_channel(
        name=nome_canal,
        category=categoria,
        overwrites=overwrites,
        topic=f"FILA:{nome_fila}|PLAYERS:{','.join(str(m.id) for m in membros if m is not None)}"
    )

    mediadores = [
        m for m in guild.members
        if any(role.name.lower() == "mediador" for role in m.roles)
    ]
    mediador = random.choice(mediadores) if mediadores else None

    if mediador:
        await canal.set_permissions(mediador, view_channel=True, send_messages=True)

    embed = discord.Embed(
        title="Aviso Importante",
        description=(
            "Mensagens, como saídas de membros ou notificações do sistema, "
            "podem aparecer no chat. Não se preocupe – elas **não afetam sua partida.**"
        ),
        color=0x111111
    )

    embed.add_field(name="🎮 Estilo de Jogo", value=nome_limpo, inline=False)
    embed.add_field(name="💰 Valor da Partida", value="Definido na fila", inline=False)

    if mediador:
        embed.add_field(name="🛡️ Mediador", value=mediador.mention, inline=False)
    else:
        embed.add_field(name="🛡️ Mediador", value="Nenhum mediador disponível", inline=False)

    jogadores_texto = "\n".join([m.mention for m in membros if m is not None])
    embed.add_field(name="👥 Jogadores", value=jogadores_texto, inline=False)

    embed.set_image(
        url="https://media.discordapp.net/attachments/1448605679785869455/1472514458940211211/Adobe_Express_-_lv_0_20260215053015.gif"
    )

    view = ConfirmacaoView([m.id for m in membros if m is not None])

    await canal.send(
        content=" ".join(m.mention for m in membros if m is not None),
        embed=embed,
        view=view
    )

    return canal


class ConfirmacaoView(discord.ui.View):
    def __init__(self, jogadores_ids):
        super().__init__(timeout=300)
        self.jogadores_ids = jogadores_ids
        self.confirmados = set()

    @discord.ui.button(label="Confirmar Resultado", style=discord.ButtonStyle.green)
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.jogadores_ids:
            await interaction.response.send_message(
                "Você não participa dessa partida.",
                ephemeral=True
            )
            return

        if interaction.user.id in self.confirmados:
            await interaction.response.send_message(
                "Você já confirmou!",
                ephemeral=True
            )
            return

        self.confirmados.add(interaction.user.id)
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} confirmou a aposta!"
        )

        if len(self.confirmados) == 2:
            await self.criar_sala_mediador(interaction)

    @discord.ui.button(label="Cancelar Partida", style=discord.ButtonStyle.red)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.jogadores_ids:
            await interaction.response.send_message(
                "Você não participa dessa partida.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "❌ Partida cancelada. Fechando sala..."
        )

        await asyncio.sleep(2)
        await interaction.channel.delete()

    async def criar_sala_mediador(self, interaction: discord.Interaction):
        canal = interaction.channel
        await canal.purge(limit=100)

        mensagem = """
Seja bem-vindo a nossa organização de Free Fire.  
Para esclarecer suas dúvidas, resolver problemas, resgatar eventos, basta selecionar a opção desejada!

✔ SUPORTE: Dúvidas, Reclamações, assistência no Discord!

✔ REEMBOLSO: Mediador pagou errado,
✔ fechou a fila, faltou algum dinheiro.

✔ RECEBER EVENTO: Você ganhou algum evento e quer receber, abra aqui.

✔ VAGAS MEDIADOR: Virar um Mediador na ORG LINKING
Abra um ticket.
        """

        embed = discord.Embed(color=discord.Color.green())
        embed.set_image(url=IMAGEM_URL)

        await canal.send(content=mensagem, embed=embed)


# =========================
# FILA
# =========================

async def entrar_fila(interaction, nome, emuladores=1):
    dados = carregar()
    user = interaction.user

    # Se a mensagem da fila existe, mas o registro sumiu do Supabase por algum reset/bug antigo,
    # recria automaticamente em vez de quebrar com "Fila não encontrada".
    fila = garantir_fila_salva(dados, nome)

    if lista_tem_usuario(fila["jogadores"], user.id):
        remover_usuario_lista(fila["jogadores"], user.id)
        if str(user.id) in fila["modo"]:
            del fila["modo"][str(user.id)]

    adicionar_usuario_lista(fila["jogadores"], user.id)
    fila["modo"][str(user.id)] = emuladores

    dados.setdefault("fila_tempo", {}).setdefault(nome, {})[str(user.id)] = time.time()

    salvar(dados)

    tipo = tipo_fila(nome)

    if tipo == "1v1_gel":
        mensagem = "✅ Você entrou na fila Gel Normal." if emuladores == 1 else "✅ Você entrou na fila Gel Infinito."
    elif tipo in ("2v2_misto", "3v3_misto", "4v4_misto"):
        mensagem = f"✅ Você entrou na fila {emuladores} Emulador(es)."
    else:
        mensagem = "✅ Você entrou na fila."

    await interaction.response.send_message(mensagem, ephemeral=True)

    necessario = 2

    jogadores_mesmo_modo = [
        uid for uid in fila["jogadores"]
        if fila["modo"].get(str(uid), 1) == emuladores
    ]

    if len(jogadores_mesmo_modo) >= necessario:
        membros = [
            guild_get_member_safe(interaction.guild, uid)
            for uid in jogadores_mesmo_modo[:2]
        ]

        for uid in jogadores_mesmo_modo[:2]:
            if lista_tem_usuario(fila["jogadores"], uid):
                remover_usuario_lista(fila["jogadores"], uid)
            if str(uid) in fila["modo"]:
                del fila["modo"][str(uid)]
            dados.setdefault("fila_tempo", {}).setdefault(nome, {}).pop(str(uid), None)

        salvar(dados)

        await criar_sala_privada(
            interaction.guild,
            membros,
            nome,
            emuladores
        )

    await atualizar_embed(interaction, nome)


async def atualizar_embed(interaction, nome):
    dados = carregar()

    fila = garantir_fila_salva(dados, nome)

    if "streamer" in fila:
        jogadores_texto = ""

        for i, user_id in enumerate(fila["jogadores"], start=1):
            membro = guild_get_member_safe(interaction.guild, user_id)
            if membro:
                jogadores_texto += f"**{i}.** {membro.mention}\n"

        if jogadores_texto == "":
            jogadores_texto = "Nenhum aguardando."

        streamer_id = fila["streamer"]
        streamer_membro = guild_get_member_safe(interaction.guild, streamer_id)

        if not streamer_membro:
            return

        embed = discord.Embed(
            title=f"🎥 JOGUE CONTRA O {streamer_membro.display_name.upper()}",
            color=discord.Color.purple()
        )

        embed.add_field(name="🎮 Formato", value=fila["formato"], inline=False)
        embed.add_field(name="💰 Valor", value=f"R$ {fila['valor']:.2f}", inline=False)
        embed.add_field(name="📜 Regras", value=fila["regras"], inline=False)
        embed.add_field(name="👥 Jogadores", value=jogadores_texto, inline=False)
        embed.set_thumbnail(url=streamer_membro.display_avatar.url)

        await interaction.message.edit(embed=embed, view=FilaStreamerView(nome))
        return

    jogadores_texto = ""

    if "modo" not in fila:
        fila["modo"] = {}

    for user_id in fila["jogadores"]:
        membro = guild_get_member_safe(interaction.guild, user_id)
        if not membro:
            continue

        modo = fila["modo"].get(str(user_id), 1)
        modo_txt = texto_modo(nome, modo)

        if modo_txt:
            jogadores_texto += f"{membro.mention} | {modo_txt}\n"
        else:
            jogadores_texto += f"{membro.mention}\n"

    if jogadores_texto == "":
        jogadores_texto = "Nenhum jogador na fila."

    embed = discord.Embed(color=0x111111)
    embed.title = f"{nome} | Fila de Competição"
    embed.add_field(name="📋 Formato", value=nome, inline=False)
    embed.add_field(name="💰 Valor", value=f"R$ {fila['valor']:.2f}", inline=False)
    embed.add_field(name="👥 Jogadores", value=jogadores_texto, inline=False)
    embed.add_field(name="🛡️ Mediador", value="Será definido na partida", inline=False)
    embed.set_thumbnail(url=IMAGEM_URL)

    await interaction.message.edit(embed=embed, view=FilaView(nome))


# =========================
# COMANDOS
# =========================

@tree.command(name="vencedor", description="Marcar vencedor da partida")
@app_commands.check(is_mediador)
async def vencedor(interaction: discord.Interaction, usuario: discord.Member):
    dados = carregar()

    user_id = str(usuario.id)
    dados["ranking"][user_id] = dados["ranking"].get(user_id, 0) + 1
    dados["coins"][user_id] = dados["coins"].get(user_id, 0) + 1

    canal = interaction.channel
    membros_perdedores = [m.id for m in canal.members if str(m.id) != user_id]

    for loser_id in membros_perdedores:
        loser_id_str = str(loser_id)
        dados["derrotas"][loser_id_str] = dados["derrotas"].get(loser_id_str, 0) + 1

    salvar(dados)

    pontos = dados["ranking"][user_id]
    coins = dados["coins"][user_id]

    await interaction.response.send_message(
        f"🏆 {usuario.mention} venceu a partida!\n"
        f"⭐ Pontuação atual: {pontos} ponto(s)\n"
        f"💰 LinCoins atuais: {coins}"
    )


@tree.command(name="puxar", description="Puxar jogador da sua fila para partida")
@app_commands.check(is_streamer)
async def puxar(interaction: discord.Interaction, jogador: discord.Member):
    dados = carregar()

    nome_fila = None
    fila = None

    for nome, info in dados["filas"].items():
        if info.get("streamer") == interaction.user.id and jogador.id in info.get("jogadores", []):
            nome_fila = nome
            fila = info
            break

    if fila is None:
        await interaction.response.send_message(
            "❌ Esse jogador não está em nenhuma fila sua.",
            ephemeral=True
        )
        return

    if jogador.id == interaction.user.id:
        await interaction.response.send_message(
            "❌ Você não pode puxar a si mesmo.",
            ephemeral=True
        )
        return

    modo = fila.get("modo", {}).get(str(jogador.id), 1)

    fila["jogadores"].remove(jogador.id)

    if "modo" in fila and str(jogador.id) in fila["modo"]:
        del fila["modo"][str(jogador.id)]

    fila["em_partida"] = True
    salvar(dados)

    await interaction.response.send_message(
        f"🎮 {jogador.mention} foi puxado da fila **{nome_fila}**."
    )

    await criar_sala_privada(
        interaction.guild,
        [interaction.user, jogador],
        nome_fila,
        modo
    )


@puxar.error
async def puxar_error(interaction: discord.Interaction, error):
    if interaction.response.is_done():
        return

    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message(
            "❌ Você precisa ter cargo de Streamer para usar esse comando.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"❌ Erro no comando /puxar: {error}",
            ephemeral=True
        )
        print(f"Erro no /puxar: {error}")


@tree.command(name="ranking", description="Ver ranking geral")
async def ranking(interaction: discord.Interaction):
    dados = carregar()

    if not dados["ranking"]:
        await interaction.response.send_message("Nenhum ponto registrado ainda.")
        return

    ranking_ordenado = sorted(dados["ranking"].items(), key=lambda x: x[1], reverse=True)
    texto = ""

    for i, (user_id, pontos) in enumerate(ranking_ordenado[:10], start=1):
        membro = interaction.guild.get_member(int(user_id))
        if membro:
            texto += f"{i}º - {membro.mention} • {pontos} ponto(s)\n"

    embed = discord.Embed(title="🏆 Ranking Geral", description=texto or "Sem jogadores.", color=0x111111)
    await interaction.response.send_message(embed=embed)


@tree.command(name="criar_fila_streamer", description="Criar fila exclusiva para streamer")
@app_commands.check(is_admin)
async def criar_fila_streamer(
    interaction: discord.Interaction,
    streamer: discord.Member,
    formato: str,
    valor: float,
    regras: str
):
    # Evita "O aplicativo não respondeu" quando Supabase/Discord demora.
    await interaction.response.defer(ephemeral=True, thinking=True)

    dados = carregar()
    nome_fila = f"JOGUE CONTRA {streamer.display_name}"

    dados["filas"][nome_fila] = {
        "valor": valor,
        "max": 50,
        "jogadores": [],
        "streamer": streamer.id,
        "formato": formato,
        "regras": regras,
        "em_partida": False,
        "modo": {},
        "salas_criadas": []
    }

    salvar(dados)

    embed = discord.Embed(
        title=f"🎥 JOGUE CONTRA O {streamer.display_name.upper()}",
        color=discord.Color.purple()
    )

    embed.add_field(name="🎮 Formato", value=formato, inline=False)
    embed.add_field(name="💰 Valor", value=f"R$ {valor:.2f}", inline=False)
    embed.add_field(name="📜 Regras", value=regras, inline=False)
    embed.add_field(name="👥 Jogadores", value="Nenhum aguardando.", inline=False)
    embed.set_thumbnail(url=streamer.display_avatar.url)

    await interaction.followup.send("Fila streamer criada com sucesso.", ephemeral=True)
    await interaction.channel.send(embed=embed, view=FilaStreamerView(nome_fila))


@criar_fila_streamer.error
async def criar_fila_streamer_error(interaction: discord.Interaction, error):
    if interaction.response.is_done():
        send = interaction.followup.send
    else:
        send = interaction.response.send_message

    if isinstance(error, app_commands.CheckFailure):
        await send("❌ Você precisa ser administrador para criar fila de streamer.", ephemeral=True)
    else:
        await send(f"❌ Erro ao criar fila de streamer: {error}", ephemeral=True)
        print(f"Erro no /criar_fila_streamer: {error}")


@tree.command(name="criar_fila", description="Criar nova fila")
@app_commands.check(is_admin)
async def criar_fila(interaction: discord.Interaction, nome: str, valor: float, max_jogadores: int):
    # Evita "O aplicativo não respondeu" quando Supabase/Discord demora.
    await interaction.response.defer(ephemeral=True, thinking=True)

    dados = carregar()

    nome_completo = f"{nome} - R${valor:.2f}"

    if nome_completo in dados["filas"]:
        await interaction.followup.send(
            "Já existe uma fila com esse formato e valor.",
            ephemeral=True
        )
        return

    dados["filas"][nome_completo] = {
        "valor": valor,
        "max": max_jogadores,
        "jogadores": [],
        "modo": {},
        "salas_criadas": []
    }

    salvar(dados)

    embed = discord.Embed(color=0x111111)
    embed.title = f"{nome_completo} | Fila de Competição"
    embed.add_field(name="📋 Formato", value=nome, inline=False)
    embed.add_field(name="💰 Valor", value=f"R$ {valor:.2f}", inline=False)
    embed.add_field(name="👥 Jogadores", value="Nenhum jogador na fila.", inline=False)
    embed.add_field(name="🛡️ Mediador", value="Será definido na partida", inline=False)
    embed.set_thumbnail(url=IMAGEM_URL)

    await interaction.followup.send("Fila criada com sucesso.", ephemeral=True)
    await interaction.channel.send(embed=embed, view=FilaView(nome_completo))


@criar_fila.error
async def criar_fila_error(interaction: discord.Interaction, error):
    if interaction.response.is_done():
        send = interaction.followup.send
    else:
        send = interaction.response.send_message

    if isinstance(error, app_commands.CheckFailure):
        await send("❌ Você precisa ser administrador para criar fila.", ephemeral=True)
    else:
        await send(f"❌ Erro ao criar fila: {error}", ephemeral=True)
        print(f"Erro no /criar_fila: {error}")


async def autocomplete_filas(interaction: discord.Interaction, current: str):
    dados = carregar()
    nomes = list(dados.get("filas", {}).keys())
    current_lower = (current or "").lower()
    filtradas = [n for n in nomes if current_lower in n.lower()]
    return [app_commands.Choice(name=n[:100], value=n) for n in filtradas[:25]]


@tree.command(name="encerrar_fila", description="Fechar sala privada e remover os jogadores dessa partida da fila")
@app_commands.check(is_admin)
@app_commands.autocomplete(nome=autocomplete_filas)
async def encerrar_fila(interaction: discord.Interaction, nome: str = None):
    await interaction.response.defer(ephemeral=True, thinking=True)

    dados = carregar()
    filas = dados.get("filas", {})

    canal_atual = interaction.channel
    topico_atual = (getattr(canal_atual, "topic", "") or "").strip()

    nome_escolhido = None
    jogadores_para_remover = []

    # Sala privada nova: FILA:nome|PLAYERS:id,id
    if topico_atual.startswith("FILA:"):
        resto = topico_atual.replace("FILA:", "", 1).strip()

        if "|PLAYERS:" in resto:
            nome_escolhido, players_txt = resto.split("|PLAYERS:", 1)
            nome_escolhido = nome_escolhido.strip()

            for parte in players_txt.split(","):
                parte = parte.strip()
                if parte.isdigit():
                    jogadores_para_remover.append(int(parte))
        else:
            nome_escolhido = resto.strip()

    if nome:
        if nome in filas:
            nome_escolhido = nome
        else:
            for fila_nome in filas.keys():
                if fila_nome.lower() == nome.lower():
                    nome_escolhido = fila_nome
                    break

            if not nome_escolhido:
                nome_escolhido = nome.strip()

    if not nome_escolhido:
        await interaction.followup.send(
            "❌ Não consegui identificar a fila. Use dentro da sala privada ou escolha o nome da fila.",
            ephemeral=True
        )
        return

    # Se estiver dentro de uma sala antiga sem PLAYERS no tópico,
    # remove os membros humanos desse canal da fila.
    if not jogadores_para_remover and topico_atual.startswith("FILA:"):
        try:
            jogadores_para_remover = [
                m.id for m in canal_atual.members
                if not m.bot and m.id != interaction.user.id
            ]
        except Exception:
            jogadores_para_remover = []

    if nome_escolhido in filas:
        fila = garantir_fila_salva(dados, nome_escolhido)

        # Se tem jogadores específicos, remove só eles.
        # Se não tem, limpa a fila inteira.
        if jogadores_para_remover:
            fila["jogadores"] = [
                uid for uid in fila.get("jogadores", [])
                if uid not in jogadores_para_remover
            ]

            if "modo" in fila:
                for uid in jogadores_para_remover:
                    fila["modo"].pop(str(uid), None)

            if "salas_criadas" in fila:
                fila["salas_criadas"] = [
                    uid for uid in fila.get("salas_criadas", [])
                    if uid not in jogadores_para_remover
                ]
        else:
            fila["jogadores"] = []
            fila["modo"] = {}
            fila["salas_criadas"] = []

        fila["em_partida"] = False
        salvar(dados)

    canais_para_deletar = []

    if topico_atual.startswith("FILA:"):
        canais_para_deletar.append(canal_atual)
    else:
        for canal in interaction.guild.text_channels:
            topico = (getattr(canal, "topic", "") or "").strip()

            if topico == f"FILA:{nome_escolhido}" or topico.startswith(f"FILA:{nome_escolhido}|PLAYERS:"):
                canais_para_deletar.append(canal)

    if not canais_para_deletar:
        await interaction.followup.send(
            f"✅ Fila **{nome_escolhido}** limpa, mas não achei sala privada aberta para fechar.",
            ephemeral=True
        )
        return

    await interaction.followup.send(
        f"✅ Encerrando {len(canais_para_deletar)} sala(s) da fila **{nome_escolhido}**.",
        ephemeral=True
    )

    for canal in canais_para_deletar:
        try:
            await canal.delete(reason=f"Fila encerrada por {interaction.user}")
        except Exception as e:
            print(f"Erro ao deletar canal {canal.name}: {e}")


@encerrar_fila.error
async def encerrar_fila_error(interaction: discord.Interaction, error):
    if interaction.response.is_done():
        send = interaction.followup.send
    else:
        send = interaction.response.send_message

    if isinstance(error, app_commands.CheckFailure):
        await send("❌ Você precisa ser administrador para encerrar fila.", ephemeral=True)
    else:
        await send(f"❌ Erro ao encerrar fila: {error}", ephemeral=True)
        print(f"Erro no /encerrar_fila: {error}")

@tree.command(name="resetar_filas", description="Resetar tudo")
@app_commands.check(is_admin)
async def resetar_filas(interaction: discord.Interaction):
    dados = dados_padrao()
    salvar(dados)
    await interaction.response.send_message("Todas as filas, coins, ranking, derrotas e loja foram resetadas.")


@tree.command(name="resetar_coins", description="Zerar as LinCoins de todos os jogadores")
@app_commands.check(is_admin)
async def resetar_coins(interaction: discord.Interaction):
    dados = carregar()

    total_jogadores = len(dados["coins"])
    for user_id in list(dados["coins"].keys()):
        dados["coins"][user_id] = 0

    salvar(dados)

    await interaction.response.send_message(
        f"💰 As LinCoins de {total_jogadores} jogador(es) foram resetadas com sucesso."
    )


@tree.command(name="painel_blacklist", description="Criar painel de verificação da blacklist")
async def painel_blacklist(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 Blacklist Local",
        description=(
            "✔ Olá, aqui é onde você vai verificar se um ID está na lista local de blacklist.\n"
            "✔ ID do Free Fire proibido de jogar na org (W.O, cheats, etc).\n"
            "✔ Utilize o botão abaixo para verificar se um usuário está na lista."
        ),
        color=discord.Color.red()
    )

    await interaction.response.send_message(embed=embed, view=BlacklistView())


@tree.command(name="perfil", description="Abrir painel de perfil do servidor")
async def perfil(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📊 Perfil",
        description=(
            "Seja Bem Vindo(a) ao painel de ranking de créditos do servidor!\n"
            "Veja o ranking atual do servidor ou seu perfil através dos botões abaixo."
        ),
        color=0x2b2d31
    )

    await interaction.response.send_message(embed=embed, view=PerfilView())


@tree.command(name="derrota", description="Adicionar derrota a um jogador")
@app_commands.check(is_admin)
async def derrota(interaction: discord.Interaction, membro: discord.Member):
    dados = carregar()
    user_id = str(membro.id)

    dados["derrotas"][user_id] = dados["derrotas"].get(user_id, 0) + 1
    salvar(dados)

    await interaction.response.send_message(f"❌ Derrota adicionada para {membro.mention}")


@tree.command(name="addblacklist", description="Adicionar ID do jogador à blacklist")
@app_commands.check(is_admin)
async def addblacklist(interaction: discord.Interaction, id_jogador: str):
    dados = carregar()

    if id_jogador in dados["blacklist"]:
        await interaction.response.send_message(
            "Esse ID já está na blacklist.",
            ephemeral=True
        )
        return

    dados["blacklist"].append(id_jogador)
    salvar(dados)

    await interaction.response.send_message(
        "ID adicionado à blacklist com sucesso.",
        ephemeral=True
    )


@tree.command(name="id_senha", description="Enviar ID e senha da sala")
@app_commands.check(is_mediador)
async def id_senha(interaction: discord.Interaction, id_sala: str, senha: str):
    embed = discord.Embed(
        title="🎮 SALA LIBERADA",
        color=discord.Color.green()
    )

    embed.add_field(name="🆔 ID", value=f"`{id_sala}`", inline=False)
    embed.add_field(name="🔐 Senha", value=f"`{senha}`", inline=False)
    embed.add_field(name="⏳ Início", value="A sala irá começar em **04:00**", inline=False)
    embed.add_field(
        name="⚠️ Atenção",
        value="Se não entrar em 4 minutos é W.O ou será cobrado valor da sala (coins).",
        inline=False
    )

    await interaction.response.send_message(embed=embed)
    mensagem = await interaction.original_response()

    tempo_total = 240

    while tempo_total > 0:
        minutos = tempo_total // 60
        segundos = tempo_total % 60

        embed.set_field_at(
            2,
            name="⏳ Início",
            value=f"A sala irá começar em **{minutos:02d}:{segundos:02d}**",
            inline=False
        )

        await mensagem.edit(embed=embed)
        await asyncio.sleep(1)
        tempo_total -= 1


@tree.command(name="config_loja", description="Adicionar item à loja")
@app_commands.check(is_admin)
async def config_loja(interaction: discord.Interaction, nome_item: str, preco: int, cargo: discord.Role):
    dados = carregar()

    dados["loja"][nome_item] = {
        "preco": preco,
        "cargo_id": cargo.id
    }

    salvar(dados)

    await interaction.response.send_message(
        f"Item **{nome_item}** adicionado à loja por {preco} LinCoins.",
        ephemeral=True
    )


@tree.command(name="remover_loja", description="Remover item da loja")
@app_commands.check(is_admin)
async def remover_loja(interaction: discord.Interaction, nome_item: str):
    dados = carregar()

    if nome_item not in dados["loja"]:
        await interaction.response.send_message(
            "❌ Esse item não existe na loja.",
            ephemeral=True
        )
        return

    del dados["loja"][nome_item]
    salvar(dados)

    await interaction.response.send_message(
        f"🗑 Item **{nome_item}** removido da loja.",
        ephemeral=True
    )


@tree.command(name="coins", description="Abrir lojinha da org")
async def coins(interaction: discord.Interaction):
    dados = carregar()
    user_id = str(interaction.user.id)
    saldo = dados["coins"].get(user_id, 0)

    embed = discord.Embed(
        title="🛒 LOJINHA ORG LINKING",
        description=(
            "Seja bem-vindo(a) à lojinha da Org Linking!\n\n"
            f"💰 Seu saldo: **{saldo} LinCoins**\n\n"
            "Você pode comprar os produtos abaixo com seus LinCoins!\n"
            "Após comprar, abra ticket em 📮・ticket para resgatar."
        ),
        color=0xFFD700
    )

    await interaction.response.send_message(embed=embed, view=LojaView())


# =========================
# COMANDOS PREFIXO
# =========================

@bot.command(name="p")
async def desempenho(ctx, usuario: discord.Member):
    dados = carregar()
    vitorias = dados["ranking"].get(str(usuario.id), 0)
    derrotas = dados["derrotas"].get(str(usuario.id), 0)
    total = vitorias + derrotas
    porcentagem = (vitorias / total * 100) if total > 0 else 0

    embed = discord.Embed(title=f"📊 Desempenho de {usuario.display_name}", color=0x00ff00)
    embed.add_field(name="🏆 Vitórias", value=str(vitorias), inline=True)
    embed.add_field(name="❌ Derrotas", value=str(derrotas), inline=True)
    embed.add_field(name="📈 % de vitórias", value=f"{porcentagem:.2f}%", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="painel")
async def painel(ctx):
    dados = carregar()

    if not dados["ranking"]:
        await ctx.send("Nenhum ranking disponível ainda.")
        return

    ranking_ordenado = sorted(dados["ranking"].items(), key=lambda x: x[1], reverse=True)[:20]
    texto = ""

    for user_id, vitorias in ranking_ordenado:
        texto += f"<@{user_id}> - {vitorias} vitórias\n"

    canal_nome = "ranking"
    guild = ctx.guild
    canal = discord.utils.get(guild.text_channels, name=canal_nome)

    if not canal:
        canal = await guild.create_text_channel(canal_nome)

    await canal.purge(limit=50)

    embed = discord.Embed(title="🏆 Top 20 Geral", description=texto, color=0xFFD700)
    await canal.send(embed=embed)


if not TOKEN:
    raise ValueError("Defina a variável de ambiente DISCORD_TOKEN antes de rodar o bot.")

app = Flask(__name__)

@app.route("/")
def home():
    return "OK", 200

def run():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()

keep_alive()

bot.run(TOKEN)
