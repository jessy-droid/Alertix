"""
╔══════════════════════════════════════════════════════════╗
║            ALERTIX v1.1 — Bot Discord (Railway)          ║
║  Fonctionnalités :                                       ║
║    • File d'attente persistante (JSON)                   ║
║    • Historique des annonces                             ║
║    • Rappel automatique aux admins                       ║
║    • /mespending pour les annonceurs                     ║
║    • Anti-spam annonceurs                                ║
║    • Modification avant approbation (/edit)              ║
╚══════════════════════════════════════════════════════════╝

Déploiement Railway :
    1. Pushez ce fichier + requirements.txt + Procfile sur GitHub
    2. Connectez le repo sur railway.app
    3. Ajoutez les variables d'environnement dans Railway :
         DISCORD_TOKEN, ADMIN_ROLE_ID, ANNOUNCE_ROLE_ID
    4. Railway démarre automatiquement le bot

⚠️  Sur Railway, les fichiers JSON (pending/history) sont
    remis à zéro à chaque redéploiement (pas de stockage
    persistant sur le filesystem). Ceci est normal.
"""

import discord
from discord.ext import commands
from discord import app_commands
import os
import json
import asyncio
from datetime import datetime, timezone

# ─── Configuration ────────────────────────────────────────────────────────────

# Les variables d'environnement sont configurées directement sur Railway.
# Pas besoin de fichier .env.
TOKEN            = os.getenv("DISCORD_TOKEN")
ADMIN_ROLE_ID    = int(os.getenv("ADMIN_ROLE_ID",    "1486324223856218132"))
ANNOUNCE_ROLE_ID = int(os.getenv("ANNOUNCE_ROLE_ID", "1486324507571388559"))

# ─── Détection environnement Railway ─────────────────────────────────────────
# Railway injecte la variable RAILWAY_ENVIRONMENT automatiquement.
ON_RAILWAY = os.getenv("RAILWAY_ENVIRONMENT") is not None

# Sur Railway, les fichiers JSON sont stockés dans /tmp (persistance session)
# pour éviter les erreurs de permission sur le filesystem en lecture seule.
if ON_RAILWAY:
    DATA_DIR = "/tmp"
else:
    DATA_DIR = "."

ANNOUNCE_CHANNEL_ID = 1485419158643671040

# Délai en heures avant rappel aux admins pour une annonce non traitée
REMINDER_DELAY_HOURS = 2

# Anti-spam : nombre max de soumissions par annonceur sur la fenêtre glissante
SPAM_MAX_SUBMISSIONS = 3
SPAM_WINDOW_SECONDS  = 3600   # 1 heure

# Fichiers de persistance
PENDING_FILE = os.path.join(DATA_DIR, "alertix_pending.json")
HISTORY_FILE = os.path.join(DATA_DIR, "alertix_history.json")

# ─── Templates ────────────────────────────────────────────────────────────────

TEMPLATES = {
    "info": {
        "emoji":  "📢",
        "couleur": 0x3498DB,
        "fields": [
            ("contenu", "Contenu de l'information à partager", True),
        ],
        "body_fn": lambda f, auteur: (
            f"@everyone\n\n🔵 | INFORMATION\n\nBonjour à tous 👋\n\n"
            f"Nous souhaitions vous informer que :\n➤ {f['contenu']}\n\n"
            f"N'hésitez pas à consulter régulièrement ce salon pour rester à jour.\n\n-{auteur}"
        ),
    },
    "événement": {
        "emoji":  "🎉",
        "couleur": 0x9B59B6,
        "fields": [
            ("date",        "Date de l'événement",   True),
            ("heure",       "Heure de l'événement",  True),
            ("lieu",        "Salon ou lieu",          True),
            ("description", "Description détaillée", True),
        ],
        "body_fn": lambda f, auteur: (
            f"@everyone\n\n🟣 | ÉVÉNEMENT\n\nUn nouvel événement arrive ! 🎊\n\n"
            f"📅 Date : {f['date']}\n⏰ Heure : {f['heure']}\n📍 Lieu : {f['lieu']}\n\n"
            f"🎯 Détails :\n➤ {f['description']}\n\nNous comptons sur votre présence 🔥\n\n-{auteur}"
        ),
    },
    "urgence": {
        "emoji":  "🚨",
        "couleur": 0xE74C3C,
        "fields": [
            ("probleme", "Description du problème",     True),
            ("action1",  "Première action à suivre",    True),
            ("action2",  "Deuxième action (optionnel)", False),
        ],
        "body_fn": lambda f, auteur: (
            f"@everyone\n\n🔴 | URGENCE\n\n⚠️ Merci de votre attention immédiate ⚠️\n\n"
            f"Un problème important a été détecté :\n➤ {f['probleme']}\n\n"
            f"👉 Actions à suivre :\n• {f['action1']}\n"
            + (f"• {f['action2']}\n" if f.get("action2") else "")
            + f"\nNous vous tiendrons informés de l'évolution.\n\n-{auteur}"
        ),
    },
    "mise à jour": {
        "emoji":  "🔄",
        "couleur": 0xF1C40F,
        "fields": [
            ("changement1", "Premier changement",               True),
            ("changement2", "Deuxième changement",              True),
            ("changement3", "Troisième changement (optionnel)", False),
        ],
        "body_fn": lambda f, auteur: (
            f"@everyone\n\n🟡 | MISE À JOUR\n\nUne nouvelle mise à jour est disponible ✨\n\n"
            f"📦 Changements :\n• {f['changement1']}\n• {f['changement2']}\n"
            + (f"• {f['changement3']}\n" if f.get("changement3") else "")
            + f"\n💬 N'hésitez pas à nous faire vos retours !\n\n-{auteur}"
        ),
    },
    "général": {
        "emoji":  "💬",
        "couleur": 0x95A5A6,
        "fields": [
            ("message", "Message général à publier", True),
        ],
        "body_fn": lambda f, auteur: (
            f"@everyone\n\n⚪ | ANNONCE\n\nBonjour à tous 👋\n\n"
            f"➤ {f['message']}\n\nMerci pour votre attention ❤️\n\n-{auteur}"
        ),
    },
}

# ─── Stockage ─────────────────────────────────────────────────────────────────

pending_announcements: dict[int, dict] = {}
history: list[dict] = []
spam_tracker: dict[int, list[float]] = {}
pending_counter = 0

# ─── Persistance JSON ─────────────────────────────────────────────────────────

def _pending_serializable(data: dict) -> dict:
    return {k: v for k, v in data.items() if k != "reminder_task"}

def save_pending():
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {str(k): _pending_serializable(v) for k, v in pending_announcements.items()},
            f, ensure_ascii=False, indent=2
        )

def load_pending():
    global pending_counter
    if not os.path.exists(PENDING_FILE):
        return
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for k, v in raw.items():
            pid = int(k)
            v["reminder_task"] = None
            pending_announcements[pid] = v
            if pid > pending_counter:
                pending_counter = pid
        print(f"  📂 {len(pending_announcements)} annonce(s) en attente rechargée(s)")
    except Exception as e:
        print(f"  ⚠️  Impossible de charger {PENDING_FILE} : {e}")

def save_history():
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history.extend(json.load(f))
        print(f"  📂 {len(history)} entrée(s) d'historique chargée(s)")
    except Exception as e:
        print(f"  ⚠️  Impossible de charger {HISTORY_FILE} : {e}")

def add_to_history(pid: int, data: dict, statut: str, admin_nom: str = "—"):
    history.append({
        "id":         pid,
        "titre":      data.get("titre", "—"),
        "type":       data.get("type", "—"),
        "auteur_nom": data.get("auteur_nom", "—"),
        "statut":     statut,
        "soumis_le":  data.get("soumis_le", "—"),
        "traite_le":  datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"),
        "admin_nom":  admin_nom,
    })
    save_history()

# ─── Intents & Bot ────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── Helpers ──────────────────────────────────────────────────────────────────

def has_role_id(member: discord.Member, role_id: int) -> bool:
    return any(r.id == role_id for r in member.roles)

def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

def check_spam(user_id: int) -> bool:
    now = datetime.now(timezone.utc).timestamp()
    timestamps = [t for t in spam_tracker.get(user_id, [])
                  if now - t < SPAM_WINDOW_SECONDS]
    spam_tracker[user_id] = timestamps
    return len(timestamps) >= SPAM_MAX_SUBMISSIONS

def register_submission(user_id: int):
    spam_tracker.setdefault(user_id, []).append(
        datetime.now(timezone.utc).timestamp())

def build_embed_from_data(data: dict) -> discord.Embed:
    tpl = TEMPLATES[data["type"]]
    embed = discord.Embed(
        title=f"{tpl['emoji']}  {data['titre']}",
        description=data["corps"],
        color=tpl["couleur"],
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Alertix • {data['auteur_nom']}")
    if data.get("image"):
        embed.set_image(url=data["image"])
    return embed

async def find_admin_channel(guild: discord.Guild):
    for ch in guild.text_channels:
        perms = ch.permissions_for(guild.me)
        if perms.send_messages and any(
            kw in ch.name.lower() for kw in ["admin", "log", "mod", "staff"]
        ):
            return ch
    return None

async def notify_admins(guild: discord.Guild, pid: int, data: dict):
    admin_role = guild.get_role(ADMIN_ROLE_ID)
    embed = discord.Embed(
        title="📬 Nouvelle annonce en attente — Alertix",
        description=(
            f"**Soumise par :** {data['auteur_nom']}\n"
            f"**Type :** {data['type']}\n"
            f"**Titre :** {data['titre']}\n"
            f"**Salon cible :** <#{ANNOUNCE_CHANNEL_ID}>\n\n"
            f"✅ `/approve id:{pid}` — publier\n"
            f"✏️ `/edit id:{pid}` — modifier\n"
            f"❌ `/reject id:{pid} raison:...` — refuser\n"
            f"👁️ `/preview id:{pid}` — prévisualiser"
        ),
        color=0xF39C12,
        timestamp=datetime.now(timezone.utc),
    )
    ch = await find_admin_channel(guild)
    if ch:
        await ch.send(content=admin_role.mention if admin_role else "", embed=embed)
    elif admin_role:
        for m in admin_role.members:
            try: await m.send(embed=embed)
            except discord.Forbidden: pass

async def send_reminder(guild: discord.Guild, pid: int):
    await asyncio.sleep(REMINDER_DELAY_HOURS * 3600)
    if pid not in pending_announcements:
        return
    data = pending_announcements[pid]
    admin_role = guild.get_role(ADMIN_ROLE_ID)
    embed = discord.Embed(
        title=f"⏰ Rappel — Annonce #{pid} toujours en attente",
        description=(
            f"Cette annonce n'a pas été traitée après **{REMINDER_DELAY_HOURS}h**.\n\n"
            f"**Titre :** {data['titre']}\n"
            f"**Type :** {data['type']}\n"
            f"**Auteur :** {data['auteur_nom']}\n"
            f"**Soumise le :** {data['soumis_le']}\n\n"
            f"✅ `/approve id:{pid}` · ❌ `/reject id:{pid}` · 👁️ `/preview id:{pid}`"
        ),
        color=0xE67E22,
        timestamp=datetime.now(timezone.utc),
    )
    ch = await find_admin_channel(guild)
    if ch:
        await ch.send(content=admin_role.mention if admin_role else "", embed=embed)
    elif admin_role:
        for m in admin_role.members:
            try: await m.send(embed=embed)
            except discord.Forbidden: pass

def schedule_reminder(guild: discord.Guild, pid: int):
    task = asyncio.create_task(send_reminder(guild, pid))
    pending_announcements[pid]["reminder_task"] = task

# ─── /announce ────────────────────────────────────────────────────────────────

@tree.command(name="announce", description="Soumettre une annonce Alertix pour validation")
@app_commands.describe(
    type_annonce="Type d'annonce",
    titre="Titre court de l'annonce",
    contenu="Texte principal (info & général)",
    date="[ÉVÉNEMENT] Date", heure="[ÉVÉNEMENT] Heure",
    lieu="[ÉVÉNEMENT] Lieu", description="[ÉVÉNEMENT] Description détaillée",
    probleme="[URGENCE] Description du problème",
    action1="[URGENCE] Première action", action2="[URGENCE] Deuxième action (optionnel)",
    changement1="[MAJ] Premier changement", changement2="[MAJ] Deuxième changement",
    changement3="[MAJ] Troisième changement (optionnel)",
    image="URL d'une image (optionnel)",
)
@app_commands.choices(type_annonce=[
    app_commands.Choice(name="📢  Info",        value="info"),
    app_commands.Choice(name="🎉  Événement",   value="événement"),
    app_commands.Choice(name="🚨  Urgence",     value="urgence"),
    app_commands.Choice(name="🔄  Mise à jour", value="mise à jour"),
    app_commands.Choice(name="💬  Général",     value="général"),
])
async def announce(
    interaction: discord.Interaction,
    type_annonce: str, titre: str,
    contenu: str | None = None, date: str | None = None,
    heure: str | None = None, lieu: str | None = None,
    description: str | None = None, probleme: str | None = None,
    action1: str | None = None, action2: str | None = None,
    changement1: str | None = None, changement2: str | None = None,
    changement3: str | None = None, image: str | None = None,
):
    member = interaction.user

    if not (has_role_id(member, ANNOUNCE_ROLE_ID) or has_role_id(member, ADMIN_ROLE_ID)):
        await interaction.response.send_message(
            "❌ Tu n'as pas la permission de soumettre une annonce.", ephemeral=True)
        return

    # Anti-spam (admins exemptés)
    if not has_role_id(member, ADMIN_ROLE_ID):
        if check_spam(member.id):
            await interaction.response.send_message(
                f"⛔ **Anti-spam** : tu as soumis {SPAM_MAX_SUBMISSIONS} annonces "
                f"en moins d'une heure. Merci de patienter avant d'en soumettre une nouvelle.",
                ephemeral=True)
            return

    # Validation des champs
    tpl = TEMPLATES[type_annonce]
    mapping = {
        "contenu": contenu, "date": date, "heure": heure, "lieu": lieu,
        "description": description, "probleme": probleme,
        "action1": action1, "action2": action2,
        "changement1": changement1, "changement2": changement2,
        "changement3": changement3, "message": contenu,
    }
    fields: dict[str, str] = {}
    missing = []
    for field_name, _, required in tpl["fields"]:
        value = mapping.get(field_name)
        if value:
            fields[field_name] = value
        elif required:
            missing.append(f"`{field_name}`")

    if missing:
        await interaction.response.send_message(
            f"❌ Champs manquants pour **{type_annonce}** : {', '.join(missing)}\n"
            f"Utilise `/aide` pour voir les champs requis.", ephemeral=True)
        return

    register_submission(member.id)

    global pending_counter
    pending_counter += 1
    pid = pending_counter

    corps = tpl["body_fn"](fields, member.display_name)

    pending_announcements[pid] = {
        "titre":         titre,
        "type":          type_annonce,
        "corps":         corps,
        "fields":        fields,
        "auteur_id":     member.id,
        "auteur_nom":    member.display_name,
        "image":         image,
        "soumis_le":     now_utc_str(),
        "reminder_task": None,
    }
    save_pending()
    schedule_reminder(interaction.guild, pid)

    preview = build_embed_from_data(pending_announcements[pid])
    preview.set_footer(text=f"ID soumission : #{pid}  •  En attente de validation")

    await interaction.response.send_message(
        f"✅ **Annonce `#{pid}` soumise !** Publication dans <#{ANNOUNCE_CHANNEL_ID}> après validation.",
        embed=preview, ephemeral=True)

    await notify_admins(interaction.guild, pid, pending_announcements[pid])


# ─── /edit ────────────────────────────────────────────────────────────────────

@tree.command(name="edit", description="Modifier une annonce en attente avant approbation")
@app_commands.describe(
    id="ID de l'annonce à modifier",
    nouveau_titre="Nouveau titre (optionnel)",
    nouveau_contenu="Nouveau contenu principal (optionnel)",
)
async def edit(
    interaction: discord.Interaction,
    id: int,
    nouveau_titre: str | None = None,
    nouveau_contenu: str | None = None,
):
    member = interaction.user
    is_admin = has_role_id(member, ADMIN_ROLE_ID)

    data = pending_announcements.get(id)
    if data is None:
        await interaction.response.send_message(
            f"❌ Aucune annonce en attente avec l'ID `#{id}`.", ephemeral=True)
        return

    if not is_admin and data["auteur_id"] != member.id:
        await interaction.response.send_message(
            "❌ Tu ne peux modifier que tes propres annonces.", ephemeral=True)
        return

    if not nouveau_titre and not nouveau_contenu:
        await interaction.response.send_message(
            "❌ Fournis au moins `nouveau_titre` ou `nouveau_contenu`.", ephemeral=True)
        return

    if nouveau_titre:
        data["titre"] = nouveau_titre

    if nouveau_contenu:
        tpl = TEMPLATES[data["type"]]
        main_field = tpl["fields"][0][0]
        data["fields"][main_field] = nouveau_contenu
        data["corps"] = tpl["body_fn"](data["fields"], data["auteur_nom"])

    save_pending()

    preview = build_embed_from_data(data)
    preview.set_footer(text=f"#{id} modifiée • En attente de validation")
    await interaction.response.send_message(
        f"✏️ **Annonce `#{id}` modifiée avec succès.** Nouvel aperçu :",
        embed=preview, ephemeral=True)

    admin_role = interaction.guild.get_role(ADMIN_ROLE_ID)
    notif = discord.Embed(
        title=f"✏️ Annonce #{id} modifiée",
        description=(
            f"**Modifiée par :** {member.mention}\n"
            f"**Nouveau titre :** {data['titre']}\n\n"
            f"✅ `/approve id:{id}` · 👁️ `/preview id:{id}` · ❌ `/reject id:{id}`"
        ),
        color=0x3498DB,
        timestamp=datetime.now(timezone.utc),
    )
    ch = await find_admin_channel(interaction.guild)
    if ch:
        await ch.send(content=admin_role.mention if admin_role else "", embed=notif)


# ─── /approve ─────────────────────────────────────────────────────────────────

@tree.command(name="approve", description="Approuver et publier une annonce en attente")
@app_commands.describe(id="ID de l'annonce à approuver")
async def approve(interaction: discord.Interaction, id: int):
    if not has_role_id(interaction.user, ADMIN_ROLE_ID):
        await interaction.response.send_message(
            "❌ Seuls les admins peuvent approuver des annonces.", ephemeral=True)
        return

    data = pending_announcements.pop(id, None)
    if data is None:
        await interaction.response.send_message(
            f"❌ Aucune annonce en attente avec l'ID `#{id}`.", ephemeral=True)
        return

    if data.get("reminder_task"):
        data["reminder_task"].cancel()

    channel = interaction.guild.get_channel(ANNOUNCE_CHANNEL_ID)
    if channel is None:
        await interaction.response.send_message(
            f"❌ Salon introuvable (ID `{ANNOUNCE_CHANNEL_ID}`).", ephemeral=True)
        return

    embed = build_embed_from_data(data)
    try:
        await channel.send(content="@everyone", embed=embed)
        save_pending()
        add_to_history(id, data, "✅ Approuvée", interaction.user.display_name)
        await interaction.response.send_message(
            f"✅ Annonce `#{id}` publiée dans {channel.mention} !", ephemeral=True)
        auteur = interaction.guild.get_member(data["auteur_id"])
        if auteur:
            try:
                await auteur.send(
                    f"🎉 Ton annonce **#{id} — {data['titre']}** a été **approuvée** "
                    f"et publiée dans {channel.mention} !")
            except discord.Forbidden: pass
    except discord.Forbidden:
        await interaction.response.send_message(
            f"❌ Permission refusée sur {channel.mention}.", ephemeral=True)


# ─── /reject ──────────────────────────────────────────────────────────────────

@tree.command(name="reject", description="Refuser une annonce en attente")
@app_commands.describe(
    id="ID de l'annonce à refuser",
    raison="Raison du refus (envoyée à l'auteur en DM)",
)
async def reject(interaction: discord.Interaction, id: int,
                 raison: str = "Aucune raison fournie"):
    if not has_role_id(interaction.user, ADMIN_ROLE_ID):
        await interaction.response.send_message(
            "❌ Seuls les admins peuvent refuser des annonces.", ephemeral=True)
        return

    data = pending_announcements.pop(id, None)
    if data is None:
        await interaction.response.send_message(
            f"❌ Aucune annonce en attente avec l'ID `#{id}`.", ephemeral=True)
        return

    if data.get("reminder_task"):
        data["reminder_task"].cancel()

    save_pending()
    add_to_history(id, data, "❌ Refusée", interaction.user.display_name)
    await interaction.response.send_message(f"🗑️ Annonce `#{id}` refusée.", ephemeral=True)

    auteur = interaction.guild.get_member(data["auteur_id"])
    if auteur:
        try:
            await auteur.send(
                f"❌ Ton annonce **#{id} — {data['titre']}** a été **refusée**.\n"
                f"**Raison :** {raison}")
        except discord.Forbidden: pass


# ─── /pending (Admin) ─────────────────────────────────────────────────────────

@tree.command(name="pending", description="[Admin] Voir toutes les annonces en attente")
async def pending(interaction: discord.Interaction):
    if not has_role_id(interaction.user, ADMIN_ROLE_ID):
        await interaction.response.send_message("❌ Réservé aux admins.", ephemeral=True)
        return

    if not pending_announcements:
        await interaction.response.send_message(
            "✅ Aucune annonce en attente.", ephemeral=True)
        return

    embed = discord.Embed(
        title="📋 Annonces en attente — Alertix",
        color=0x3498DB,
        timestamp=datetime.now(timezone.utc),
    )
    for pid, data in pending_announcements.items():
        tpl = TEMPLATES[data["type"]]
        embed.add_field(
            name=f"{tpl['emoji']}  #{pid} — {data['titre']}",
            value=(
                f"**Auteur :** {data['auteur_nom']}\n"
                f"**Type :** {data['type']}\n"
                f"**Soumise le :** {data['soumis_le']}"
            ),
            inline=False,
        )
    embed.set_footer(text=f"{len(pending_announcements)} annonce(s) en attente")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── /mespending (Annonceur) ──────────────────────────────────────────────────

@tree.command(name="mespending", description="Voir tes propres annonces en attente")
async def mespending(interaction: discord.Interaction):
    member = interaction.user
    if not (has_role_id(member, ANNOUNCE_ROLE_ID) or has_role_id(member, ADMIN_ROLE_ID)):
        await interaction.response.send_message(
            "❌ Tu n'as pas accès à cette commande.", ephemeral=True)
        return

    mes = {pid: data for pid, data in pending_announcements.items()
           if data["auteur_id"] == member.id}

    if not mes:
        await interaction.response.send_message(
            "✅ Tu n'as aucune annonce en attente.", ephemeral=True)
        return

    embed = discord.Embed(
        title="📋 Mes annonces en attente",
        color=0x9B59B6,
        timestamp=datetime.now(timezone.utc),
    )
    for pid, data in mes.items():
        tpl = TEMPLATES[data["type"]]
        embed.add_field(
            name=f"{tpl['emoji']}  #{pid} — {data['titre']}",
            value=(
                f"**Type :** {data['type']}\n"
                f"**Soumise le :** {data['soumis_le']}\n"
                f"*En attente de validation admin*"
            ),
            inline=False,
        )
    embed.set_footer(text=f"{len(mes)} annonce(s) en attente • Alertix v1.1")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── /preview ─────────────────────────────────────────────────────────────────

@tree.command(name="preview", description="Prévisualiser une annonce en attente")
@app_commands.describe(id="ID de l'annonce à prévisualiser")
async def preview(interaction: discord.Interaction, id: int):
    if not has_role_id(interaction.user, ADMIN_ROLE_ID):
        await interaction.response.send_message("❌ Réservé aux admins.", ephemeral=True)
        return

    data = pending_announcements.get(id)
    if data is None:
        await interaction.response.send_message(
            f"❌ Aucune annonce avec l'ID `#{id}`.", ephemeral=True)
        return

    embed = build_embed_from_data(data)
    await interaction.response.send_message(
        f"👁️ **Prévisualisation `#{id}`** — publication dans <#{ANNOUNCE_CHANNEL_ID}> :",
        embed=embed, ephemeral=True)


# ─── /historique ──────────────────────────────────────────────────────────────

@tree.command(name="historique", description="Voir l'historique des annonces traitées")
@app_commands.describe(limite="Nombre d'entrées à afficher (défaut 10, max 25)")
async def historique(interaction: discord.Interaction, limite: int = 10):
    if not has_role_id(interaction.user, ADMIN_ROLE_ID):
        await interaction.response.send_message("❌ Réservé aux admins.", ephemeral=True)
        return

    if not history:
        await interaction.response.send_message(
            "📭 Aucune annonce dans l'historique.", ephemeral=True)
        return

    limite = min(max(limite, 1), 25)
    recentes = history[-limite:][::-1]

    embed = discord.Embed(
        title=f"📜 Historique — {len(history)} annonce(s) au total",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    for entry in recentes:
        tpl = TEMPLATES.get(entry["type"], {})
        emoji = tpl.get("emoji", "📌")
        embed.add_field(
            name=f"{emoji}  #{entry['id']} — {entry['titre']}",
            value=(
                f"**Statut :** {entry['statut']}\n"
                f"**Auteur :** {entry['auteur_nom']}\n"
                f"**Admin :** {entry['admin_nom']}\n"
                f"**Soumise :** {entry['soumis_le']}\n"
                f"**Traitée :** {entry['traite_le']}"
            ),
            inline=False,
        )
    embed.set_footer(text=f"{len(recentes)} dernières affichées • Alertix v1.1")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── /aide ────────────────────────────────────────────────────────────────────

@tree.command(name="aide", description="Afficher l'aide et les champs requis par type")
async def aide(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Alertix v1.1 — Guide des commandes",
        description="Champs requis par type d'annonce dans `/announce`.",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    for type_name, tpl in TEMPLATES.items():
        champs = "\n".join(
            f"{'✅' if req else '🔘'} `{name}` — {desc}"
            for name, desc, req in tpl["fields"]
        )
        embed.add_field(
            name=f"{tpl['emoji']}  {type_name.capitalize()}",
            value=champs, inline=False)

    embed.add_field(
        name="─────────────────",
        value=(
            "✅ = obligatoire  •  🔘 = optionnel\n"
            f"📣 Salon de publication : <#{ANNOUNCE_CHANNEL_ID}>\n"
            f"⛔ Anti-spam : max {SPAM_MAX_SUBMISSIONS} soumissions / heure\n"
            f"⏰ Rappel admin si non traité après {REMINDER_DELAY_HOURS}h"
        ),
        inline=False,
    )
    embed.set_footer(text="Alertix v1.1")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    load_pending()
    load_history()
    for guild in bot.guilds:
        for pid in list(pending_announcements.keys()):
            schedule_reminder(guild, pid)
    await tree.sync()
    print(f"")
    print(f"  ╔═══════════════════════════════════════════╗")
    env_label = 'Railway ☁️ ' if ON_RAILWAY else 'Local 💻    '
    print(f"  ║     ALERTIX v1.1 — {env_label} — En ligne ✅  ║")
    print(f"  ╠═══════════════════════════════════════════╣")
    print(f"  ║  Bot         : {bot.user}")
    print(f"  ║  Admin ID    : {ADMIN_ROLE_ID}")
    print(f"  ║  Annonceur   : {ANNOUNCE_ROLE_ID}")
    print(f"  ║  Salon       : {ANNOUNCE_CHANNEL_ID}")
    print(f"  ║  Rappel      : {REMINDER_DELAY_HOURS}h")
    print(f"  ║  Anti-spam   : {SPAM_MAX_SUBMISSIONS} soumissions / heure")
    print(f"  ╚═══════════════════════════════════════════╝")
    print(f"")

# ─── Lancement ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN manquant.")
        print("   → En local   : ajoutez-le dans le fichier .env")
        print("   → Sur Railway: ajoutez-le dans Variables d'environnement")
        raise SystemExit(1)

    print(f"  🚀 Démarrage {'sur Railway' if ON_RAILWAY else 'en local'}...")
    bot.run(TOKEN, log_handler=None)
