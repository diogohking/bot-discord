import os
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hospital-hp")

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "hospital_hp.sqlite3")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0") or 0)
MANAGEMENT_ROLE_ID = int(os.getenv("MANAGEMENT_ROLE_ID", "0") or 0)
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0") or 0)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0") or 0)
APPLICATION_CHANNEL_ID = int(os.getenv("APPLICATION_CHANNEL_ID", "0") or 0)
BRAND_RED = discord.Color.from_rgb(190, 25, 45)
BRAND_DARK = discord.Color.from_rgb(28, 28, 32)

intents = discord.Intents.default()
intents.members = True
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            duration_seconds INTEGER
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            closed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            character_name TEXT NOT NULL,
            age TEXT NOT NULL,
            experience TEXT NOT NULL,
            motivation TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pendente',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS promotion_criteria (
            role_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            required_hours INTEGER NOT NULL,
            updated_by INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(role_id, guild_id)
        );
        """)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_management(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    return bool(MANAGEMENT_ROLE_ID and any(r.id == MANAGEMENT_ROLE_ID for r in getattr(interaction.user, "roles", [])))


def has_staff_role(member) -> bool:
    return bool(STAFF_ROLE_ID and any(r.id == STAFF_ROLE_ID for r in getattr(member, "roles", [])))


async def send_log(guild: discord.Guild, title: str, description: str):
    if not LOG_CHANNEL_ID:
        return
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel:
        embed = discord.Embed(title=title, description=description, color=BRAND_RED, timestamp=datetime.now(timezone.utc))
        await channel.send(embed=embed)


def hospital_embed(title: str, description: str):
    e = discord.Embed(title=f"🏥 {title}", description=description, color=BRAND_RED)
    e.set_footer(text="Hospital HP • Sistema de Gestão")
    return e


class ApplicationModal(discord.ui.Modal, title="Candidatura ao Hospital HP"):
    character = discord.ui.TextInput(label="Nome da personagem", max_length=80)
    age = discord.ui.TextInput(label="Idade da personagem", max_length=20)
    experience = discord.ui.TextInput(label="Experiência na área", style=discord.TextStyle.paragraph, max_length=700)
    motivation = discord.ui.TextInput(label="Porque queres entrar no HP?", style=discord.TextStyle.paragraph, max_length=700)

    async def on_submit(self, interaction: discord.Interaction):
        with db() as con:
            cur = con.execute(
                "INSERT INTO applications(user_id,guild_id,character_name,age,experience,motivation,created_at) VALUES(?,?,?,?,?,?,?)",
                (interaction.user.id, interaction.guild_id, str(self.character), str(self.age),
                 str(self.experience), str(self.motivation), now_iso())
            )
            app_id = cur.lastrowid
        embed = hospital_embed(f"Candidatura #{app_id}", f"**Candidato:** {interaction.user.mention}\n**Personagem:** {self.character}\n**Idade:** {self.age}\n\n**Experiência**\n{self.experience}\n\n**Motivação**\n{self.motivation}")
        view = ApplicationReviewView(app_id)
        target = interaction.guild.get_channel(APPLICATION_CHANNEL_ID) if APPLICATION_CHANNEL_ID else None
        if target:
            await target.send(embed=embed, view=view)
        await interaction.response.send_message("A tua candidatura foi enviada à direção do Hospital HP.", ephemeral=True)
        await send_log(interaction.guild, "Nova candidatura", f"Candidatura #{app_id} enviada por {interaction.user.mention}.")


class ReportModal(discord.ui.Modal, title="Novo relatório hospitalar"):
    category = discord.ui.TextInput(label="Tipo de relatório", placeholder="Ex.: Atendimento, ocorrência, turno...", max_length=80)
    content = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, max_length=1800)

    async def on_submit(self, interaction: discord.Interaction):
        with db() as con:
            cur = con.execute(
                "INSERT INTO reports(user_id,guild_id,category,content,created_at) VALUES(?,?,?,?,?)",
                (interaction.user.id, interaction.guild_id, str(self.category), str(self.content), now_iso())
            )
            report_id = cur.lastrowid
        await interaction.response.send_message(f"Relatório **#{report_id}** registado com sucesso.", ephemeral=True)
        await send_log(interaction.guild, "Novo relatório", f"**#{report_id} — {self.category}**\nAutor: {interaction.user.mention}\n{self.content}")


class TicketCreateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Abrir atendimento", style=discord.ButtonStyle.danger, emoji="🎫", custom_id="hp:ticket:create")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        with db() as con:
            existing = con.execute("SELECT channel_id FROM tickets WHERE user_id=? AND guild_id=? AND closed_at IS NULL",
                                   (interaction.user.id, interaction.guild_id)).fetchone()
        if existing:
            channel = interaction.guild.get_channel(existing["channel_id"])
            if channel:
                await interaction.response.send_message(f"Já tens um atendimento aberto: {channel.mention}", ephemeral=True)
                return

        category = interaction.guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        if STAFF_ROLE_ID:
            role = interaction.guild.get_role(STAFF_ROLE_ID)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        channel = await interaction.guild.create_text_channel(
            name=f"atendimento-{interaction.user.name}".lower().replace(" ", "-")[:90],
            category=category, overwrites=overwrites, topic=f"Atendimento HP de {interaction.user} ({interaction.user.id})"
        )
        with db() as con:
            con.execute("INSERT INTO tickets(channel_id,user_id,guild_id,kind,created_at) VALUES(?,?,?,?,?)",
                        (channel.id, interaction.user.id, interaction.guild_id, "Atendimento", now_iso()))
        await channel.send(
            content=f"{interaction.user.mention}" + (f" <@&{STAFF_ROLE_ID}>" if STAFF_ROLE_ID else ""),
            embed=hospital_embed("Atendimento iniciado", "Explica o motivo do teu contacto. Um membro da equipa irá ajudar-te."),
            view=TicketCloseView()
        )
        await interaction.response.send_message(f"Atendimento criado: {channel.mention}", ephemeral=True)
        await send_log(interaction.guild, "Atendimento aberto", f"{channel.mention} — {interaction.user.mention}")


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fechar atendimento", style=discord.ButtonStyle.secondary, emoji="🔒", custom_id="hp:ticket:close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        with db() as con:
            row = con.execute("SELECT user_id FROM tickets WHERE channel_id=? AND closed_at IS NULL", (interaction.channel_id,)).fetchone()
        if not row:
            await interaction.response.send_message("Este canal não está registado como atendimento ativo.", ephemeral=True)
            return
        if interaction.user.id != row["user_id"] and not is_management(interaction):
            if not has_staff_role(interaction.user):
                await interaction.response.send_message("Não tens permissão para fechar este atendimento.", ephemeral=True)
                return
        with db() as con:
            con.execute("UPDATE tickets SET closed_at=? WHERE channel_id=?", (now_iso(), interaction.channel_id))
        await interaction.response.send_message("Atendimento fechado. O canal será eliminado em 5 segundos.")
        await send_log(interaction.guild, "Atendimento fechado", f"Canal #{interaction.channel.name} fechado por {interaction.user.mention}.")
        import asyncio
        await asyncio.sleep(5)
        await interaction.channel.delete(reason="Atendimento HP fechado")


class ApplicationReviewView(discord.ui.View):
    def __init__(self, app_id: int):
        super().__init__(timeout=None)
        self.app_id = app_id
        for item in self.children:
            item.custom_id = f"hp:application:{app_id}:{item.label.lower()}"

    async def _review(self, interaction: discord.Interaction, status: str):
        if not is_management(interaction):
            await interaction.response.send_message("Apenas a direção pode avaliar candidaturas.", ephemeral=True)
            return
        with db() as con:
            con.execute("UPDATE applications SET status=? WHERE id=?", (status, self.app_id))
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"Candidatura #{self.app_id}: **{status}**.", ephemeral=True)
        await send_log(interaction.guild, "Candidatura avaliada", f"Candidatura #{self.app_id}: **{status}** por {interaction.user.mention}.")

    @discord.ui.button(label="Aprovar", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._review(interaction, "Aprovada")

    @discord.ui.button(label="Rejeitar", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._review(interaction, "Rejeitada")


@bot.event
async def on_ready():
    init_db()
    bot.add_view(TicketCreateView())
    bot.add_view(TicketCloseView())
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            synced = await bot.tree.sync(guild=guild)
            log.info("Sincronizados %s comandos na guild %s", len(synced), GUILD_ID)
        else:
            synced = await bot.tree.sync()
            log.info("Sincronizados %s comandos globalmente", len(synced))
    except Exception:
        log.exception("Erro ao sincronizar comandos")
    log.info("Ligado como %s", bot.user)


@bot.tree.command(name="painel", description="Publica o painel principal do Hospital HP.")
@app_commands.guild_only()
async def painel(interaction: discord.Interaction):
    if not is_management(interaction):
        await interaction.response.send_message("Apenas a direção pode publicar o painel.", ephemeral=True)
        return
    embed = hospital_embed("Central de Serviços", "Bem-vindo ao sistema oficial do Hospital HP.\n\n🎫 **Atendimento:** abre um canal privado com a equipa.\n📝 **Candidatura:** envia a tua inscrição para integrar o HP.\n📋 **Relatórios:** regista ocorrências e atividade hospitalar.\n\nMantém uma comunicação respeitosa e segue as normas da cidade.")
    await interaction.channel.send(embed=embed, view=MainPanelView())
    await interaction.response.send_message("Painel publicado.", ephemeral=True)


class MainPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketPanelButton())
        self.add_item(ApplicationPanelButton())
        self.add_item(ReportPanelButton())


class TicketPanelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Atendimento", style=discord.ButtonStyle.danger, emoji="🎫", custom_id="hp:panel:ticket")
    async def callback(self, interaction):
        await TicketCreateView().children[0].callback(interaction)


class ApplicationPanelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Candidatar-me", style=discord.ButtonStyle.primary, emoji="📝", custom_id="hp:panel:application")
    async def callback(self, interaction):
        await interaction.response.send_modal(ApplicationModal())


class ReportPanelButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Relatório", style=discord.ButtonStyle.secondary, emoji="📋", custom_id="hp:panel:report")
    async def callback(self, interaction):
        if not (has_staff_role(interaction.user) or is_management(interaction)):
            await interaction.response.send_message("Esta função está disponível apenas para funcionários do HP.", ephemeral=True)
            return
        await interaction.response.send_modal(ReportModal())


@bot.tree.command(name="servico", description="Regista a entrada ou saída de serviço.")
@app_commands.describe(acao="Escolhe entrar ou sair de serviço")
@app_commands.choices(acao=[
    app_commands.Choice(name="Entrar", value="entrar"),
    app_commands.Choice(name="Sair", value="sair")
])
@app_commands.guild_only()
async def servico(interaction: discord.Interaction, acao: app_commands.Choice[str]):
    if not (has_staff_role(interaction.user) or is_management(interaction)):
        await interaction.response.send_message("Só funcionários do HP podem usar este comando.", ephemeral=True)
        return
    if acao.value == "entrar":
        with db() as con:
            active = con.execute("SELECT id FROM shifts WHERE user_id=? AND guild_id=? AND ended_at IS NULL",
                                 (interaction.user.id, interaction.guild_id)).fetchone()
            if active:
                await interaction.response.send_message("Já tens um turno ativo.", ephemeral=True)
                return
            con.execute("INSERT INTO shifts(user_id,guild_id,started_at) VALUES(?,?,?)",
                        (interaction.user.id, interaction.guild_id, now_iso()))
        await interaction.response.send_message("🟢 Entrada de serviço registada.", ephemeral=True)
        await send_log(interaction.guild, "Entrada de serviço", f"{interaction.user.mention} iniciou serviço.")
    else:
        with db() as con:
            active = con.execute("SELECT id,started_at FROM shifts WHERE user_id=? AND guild_id=? AND ended_at IS NULL",
                                 (interaction.user.id, interaction.guild_id)).fetchone()
            if not active:
                await interaction.response.send_message("Não tens nenhum turno ativo.", ephemeral=True)
                return
            start = datetime.fromisoformat(active["started_at"])
            end = datetime.now(timezone.utc)
            seconds = max(0, int((end - start).total_seconds()))
            con.execute("UPDATE shifts SET ended_at=?,duration_seconds=? WHERE id=?",
                        (end.isoformat(timespec="seconds"), seconds, active["id"]))
        hours, rem = divmod(seconds, 3600)
        minutes = rem // 60
        await interaction.response.send_message(f"🔴 Saída registada. Duração do turno: **{hours}h {minutes}min**.", ephemeral=True)
        await send_log(interaction.guild, "Saída de serviço", f"{interaction.user.mention} terminou serviço após **{hours}h {minutes}min**.")


@bot.tree.command(name="meuhorario", description="Consulta as tuas horas de serviço.")
@app_commands.guild_only()
async def meuhorario(interaction: discord.Interaction):
    with db() as con:
        row = con.execute("SELECT COALESCE(SUM(duration_seconds),0) total FROM shifts WHERE user_id=? AND guild_id=?",
                          (interaction.user.id, interaction.guild_id)).fetchone()
        active = con.execute("SELECT started_at FROM shifts WHERE user_id=? AND guild_id=? AND ended_at IS NULL",
                             (interaction.user.id, interaction.guild_id)).fetchone()
    total = row["total"]
    if active:
        total += max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(active["started_at"])).total_seconds()))
    hours, rem = divmod(total, 3600)
    await interaction.response.send_message(f"⏱️ Total registado: **{hours}h {rem//60}min**" + (" (inclui turno ativo)." if active else "."), ephemeral=True)


@bot.tree.command(name="estatisticas", description="Consulta as estatísticas do Hospital HP.")
@app_commands.guild_only()
async def estatisticas(interaction: discord.Interaction):
    if not is_management(interaction):
        await interaction.response.send_message("Apenas a direção pode consultar as estatísticas.", ephemeral=True)
        return
    with db() as con:
        staff = con.execute("SELECT COUNT(DISTINCT user_id) n FROM shifts WHERE guild_id=?", (interaction.guild_id,)).fetchone()["n"]
        active = con.execute("SELECT COUNT(*) n FROM shifts WHERE guild_id=? AND ended_at IS NULL", (interaction.guild_id,)).fetchone()["n"]
        tickets = con.execute("SELECT COUNT(*) n FROM tickets WHERE guild_id=? AND closed_at IS NULL", (interaction.guild_id,)).fetchone()["n"]
        pending = con.execute("SELECT COUNT(*) n FROM applications WHERE guild_id=? AND status='Pendente'", (interaction.guild_id,)).fetchone()["n"]
        reports = con.execute("SELECT COUNT(*) n FROM reports WHERE guild_id=?", (interaction.guild_id,)).fetchone()["n"]
    embed = hospital_embed("Estatísticas", f"👥 Funcionários com registo: **{staff}**\n🟢 Em serviço: **{active}**\n🎫 Atendimentos abertos: **{tickets}**\n📝 Candidaturas pendentes: **{pending}**\n📋 Relatórios submetidos: **{reports}**")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="anunciar", description="Envia um anúncio oficial do Hospital HP.")
@app_commands.describe(titulo="Título do anúncio", mensagem="Conteúdo do anúncio", canal="Canal de destino (opcional)")
@app_commands.guild_only()
async def anunciar(interaction: discord.Interaction, titulo: str, mensagem: str, canal: Optional[discord.TextChannel] = None):
    if not is_management(interaction):
        await interaction.response.send_message("Apenas a direção pode publicar anúncios.", ephemeral=True)
        return
    target = canal or interaction.channel
    embed = hospital_embed(titulo, mensagem)
    embed.timestamp = datetime.now(timezone.utc)
    await target.send(embed=embed)
    await interaction.response.send_message(f"Anúncio enviado em {target.mention}.", ephemeral=True)


@bot.tree.command(name="criteriopromocao", description="Define as horas mínimas para um cargo.")
@app_commands.describe(cargo="Cargo que terá este requisito", horas="Horas mínimas de serviço concluídas")
@app_commands.guild_only()
async def criteriopromocao(interaction: discord.Interaction, cargo: discord.Role, horas: app_commands.Range[int, 0, 10000]):
    if not is_management(interaction):
        await interaction.response.send_message("Apenas a direção pode definir critérios de promoção.", ephemeral=True)
        return
    with db() as con:
        con.execute(
            """INSERT INTO promotion_criteria(role_id,guild_id,required_hours,updated_by,updated_at)
               VALUES(?,?,?,?,?) ON CONFLICT(role_id,guild_id)
               DO UPDATE SET required_hours=excluded.required_hours, updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
            (cargo.id, interaction.guild_id, int(horas), interaction.user.id, now_iso())
        )
    await interaction.response.send_message(
        f"Critério atualizado: para ser considerado elegível a **{cargo.name}**, são necessárias **{horas} horas** de serviço registadas. A promoção continua a depender da decisão da direção.",
        ephemeral=True
    )
    await send_log(interaction.guild, "Critério de promoção atualizado",
                   f"{interaction.user.mention} definiu **{horas} horas** para o cargo {cargo.mention}.")


@bot.tree.command(name="elegibilidade", description="Verifica se um funcionário reúne o requisito de horas para um cargo.")
@app_commands.describe(funcionario="Funcionário a avaliar", cargo="Cargo pretendido")
@app_commands.guild_only()
async def elegibilidade(interaction: discord.Interaction, funcionario: discord.Member, cargo: discord.Role):
    with db() as con:
        criterion = con.execute(
            "SELECT required_hours FROM promotion_criteria WHERE role_id=? AND guild_id=?",
            (cargo.id, interaction.guild_id)
        ).fetchone()
        total_row = con.execute(
            "SELECT COALESCE(SUM(duration_seconds),0) total FROM shifts WHERE user_id=? AND guild_id=?",
            (funcionario.id, interaction.guild_id)
        ).fetchone()
        active = con.execute(
            "SELECT started_at FROM shifts WHERE user_id=? AND guild_id=? AND ended_at IS NULL",
            (funcionario.id, interaction.guild_id)
        ).fetchone()
    if not criterion:
        await interaction.response.send_message(
            f"A direção ainda não definiu um requisito de horas para **{cargo.name}**.",
            ephemeral=True
        )
        return
    seconds = int(total_row["total"])
    if active:
        seconds += max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(active["started_at"])).total_seconds()))
    worked_hours = seconds / 3600
    required = int(criterion["required_hours"])
    eligible = worked_hours >= required
    status = "Requisito de horas cumprido" if eligible else "Ainda não reúne o requisito de horas"
    embed = hospital_embed("Avaliação de promoção",
        f"**Funcionário:** {funcionario.mention}\n"
        f"**Cargo pretendido:** {cargo.mention}\n"
        f"**Horas registadas:** {worked_hours:.1f} h\n"
        f"**Horas necessárias:** {required} h\n\n"
        f"{'✅' if eligible else '⏳'} **{status}**\n\n"
        "Esta verificação é uma recomendação automática. A promoção só acontece após aprovação manual da direção.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="promover", description="A direção atribui manualmente um cargo após avaliação.")
@app_commands.describe(funcionario="Funcionário a promover", cargo="Cargo a atribuir", motivo="Motivo/justificação da decisão")
@app_commands.guild_only()
async def promover(interaction: discord.Interaction, funcionario: discord.Member, cargo: discord.Role, motivo: str):
    if not is_management(interaction):
        await interaction.response.send_message("Apenas a direção pode aprovar promoções.", ephemeral=True)
        return
    if cargo >= interaction.guild.me.top_role:
        await interaction.response.send_message("Não consigo atribuir esse cargo: coloca o cargo do bot acima dele na hierarquia do Discord.", ephemeral=True)
        return
    try:
        await funcionario.add_roles(cargo, reason=f"Promoção HP aprovada por {interaction.user}: {motivo}")
    except discord.Forbidden:
        await interaction.response.send_message("Não tenho permissões para atribuir esse cargo. Verifica a hierarquia e a permissão Gerir Cargos.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"✅ {funcionario.mention} recebeu o cargo {cargo.mention}. Promoção aprovada pela direção.",
        ephemeral=True
    )
    await send_log(interaction.guild, "Promoção aprovada",
                   f"**Funcionário:** {funcionario.mention}\n**Novo cargo:** {cargo.mention}\n**Aprovada por:** {interaction.user.mention}\n**Motivo:** {motivo}")


@bot.tree.command(name="rebaixar", description="A direção remove um cargo de um funcionário.")
@app_commands.describe(funcionario="Funcionário", cargo="Cargo a remover", motivo="Motivo/justificação")
@app_commands.guild_only()
async def rebaixar(interaction: discord.Interaction, funcionario: discord.Member, cargo: discord.Role, motivo: str):
    if not is_management(interaction):
        await interaction.response.send_message("Apenas a direção pode efetuar rebaixamentos.", ephemeral=True)
        return
    if cargo >= interaction.guild.me.top_role:
        await interaction.response.send_message("Não consigo remover esse cargo: coloca o cargo do bot acima dele na hierarquia do Discord.", ephemeral=True)
        return
    try:
        await funcionario.remove_roles(cargo, reason=f"Rebaixamento HP aprovado por {interaction.user}: {motivo}")
    except discord.Forbidden:
        await interaction.response.send_message("Não tenho permissões para remover esse cargo. Verifica a hierarquia e a permissão Gerir Cargos.", ephemeral=True)
        return
    await interaction.response.send_message(f"Rebaixamento registado para {funcionario.mention}.", ephemeral=True)
    await send_log(interaction.guild, "Rebaixamento registado",
                   f"**Funcionário:** {funcionario.mention}\n**Cargo removido:** {cargo.mention}\n**Aprovado por:** {interaction.user.mention}\n**Motivo:** {motivo}")


@bot.tree.command(name="ajuda", description="Mostra os comandos disponíveis do Hospital HP.")
@app_commands.guild_only()
async def ajuda(interaction: discord.Interaction):
    description = (
        "**Funcionários**\n`/servico entrar` — iniciar turno\n`/servico sair` — terminar turno\n`/meuhorario` — consultar horas\n"
        "**Direção**\n`/painel` — publicar painel\n`/estatisticas` — ver indicadores\n`/anunciar` — publicar comunicado\n"
        "`/criteriopromocao` — definir horas mínimas por cargo\n`/elegibilidade` — avaliar requisitos de um funcionário\n`/promover` — aprovar e atribuir cargo\n`/rebaixar` — remover cargo\n"
        "**Todos**\n`/ajuda` — ver esta lista\n\nUsa o painel do HP para atendimentos, candidaturas e relatórios."
    )
    await interaction.response.send_message(embed=hospital_embed("Ajuda", description), ephemeral=True)


if not TOKEN:
    raise RuntimeError("Falta configurar DISCORD_TOKEN no ficheiro .env")
bot.run(TOKEN)
