"""Paramètres (RH) : départements, projets, grille KPI, comptes utilisateurs."""
import secrets
import string

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import (Affectation, Critere, Departement, Employe,
                      EvaluationNote, Projet, Role, Utilisateur)
from ..permissions import rh_requis
from ..services.audit import journaliser
from ..utils import lire_date

bp = Blueprint("parametres", __name__, url_prefix="/parametres")


def mot_de_passe_temporaire() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        mdp = "".join(secrets.choice(alphabet) for _ in range(12))
        if any(c.isdigit() for c in mdp) and any(c.isalpha() for c in mdp):
            return mdp


def _employes_actifs():
    return db.session.scalars(select(Employe).where(Employe.actif.is_(True)).order_by(Employe.nom, Employe.prenom)).all()


# --- Départements & projets -------------------------------------------------
def _gerer_contexte(model, modele_tpl, titre, avec_projet=False):
    erreurs, donnees = {}, {}
    edition = None
    if request.method == "POST":
        action = request.form.get("action", "enregistrer")
        obj_id = request.form.get("id", type=int)
        obj = db.session.get(model, obj_id) if obj_id else None
        if action == "basculer" and obj:
            obj.actif = not obj.actif
            journaliser(f"{model.__tablename__}_statut", obj.nom, "actif" if obj.actif else "inactif")
            db.session.commit()
            flash(f"« {obj.nom} » {'réactivé' if obj.actif else 'archivé'}.", "succes")
            return redirect(request.path)
        donnees = {k: (request.form.get(k) or "").strip() for k in
                   ("code", "nom", "responsable_id", "localisation", "date_debut", "date_fin")}
        code = donnees["code"].upper()
        if not code:
            erreurs["code"] = "Le code est obligatoire."
        elif db.session.scalar(select(model.id).where(model.code == code, model.id != (obj.id if obj else -1))):
            erreurs["code"] = "Ce code existe déjà."
        if not donnees["nom"]:
            erreurs["nom"] = "Le nom est obligatoire."
        elif model is Departement and db.session.scalar(
                select(model.id).where(func.lower(model.nom) == donnees["nom"].lower(), model.id != (obj.id if obj else -1))):
            erreurs["nom"] = "Ce département existe déjà."
        dd = df = None
        if avec_projet:
            try:
                dd = lire_date(donnees["date_debut"])
            except ValueError as exc:
                erreurs["date_debut"] = str(exc)
            try:
                df = lire_date(donnees["date_fin"])
            except ValueError as exc:
                erreurs["date_fin"] = str(exc)
            if dd and df and df < dd:
                erreurs["date_fin"] = "La date de fin doit suivre la date de début."
        if not erreurs:
            nouveau = obj is None
            obj = obj or model()
            obj.code, obj.nom = code, donnees["nom"]
            obj.responsable_id = int(donnees["responsable_id"]) if donnees["responsable_id"] else None
            if avec_projet:
                obj.localisation = donnees["localisation"] or None
                obj.date_debut, obj.date_fin = dd, df
            if nouveau:
                db.session.add(obj)
            journaliser(f"{model.__tablename__}_{'cree' if nouveau else 'modifie'}", obj.nom, code)
            db.session.commit()
            flash(f"« {obj.nom} » enregistré.", "succes")
            return redirect(request.path)
        edition = obj
    elif request.args.get("modifier", type=int):
        edition = db.session.get(model, request.args.get("modifier", type=int)) or abort(404)
        donnees = {"code": edition.code, "nom": edition.nom,
                   "responsable_id": str(edition.responsable_id or ""),
                   "localisation": getattr(edition, "localisation", "") or "",
                   "date_debut": edition.date_debut.isoformat() if getattr(edition, "date_debut", None) else "",
                   "date_fin": edition.date_fin.isoformat() if getattr(edition, "date_fin", None) else ""}

    col = Affectation.departement_id if model is Departement else Affectation.projet_id
    effectifs = dict(db.session.execute(
        select(col, func.count(Affectation.id)).where(Affectation.actif.is_(True), col.isnot(None)).group_by(col)).all())
    elements = db.session.scalars(select(model).options(joinedload(model.responsable))
                                  .order_by(model.actif.desc(), model.nom)).all()
    return render_template(modele_tpl, elements=elements, effectifs=effectifs, edition=edition,
                           donnees=donnees, erreurs=erreurs, employes=_employes_actifs(),
                           titre=titre, avec_projet=avec_projet)


@bp.route("/departements", methods=["GET", "POST"])
@login_required
@rh_requis
def departements():
    return _gerer_contexte(Departement, "parametres/contextes.html", "Départements")


@bp.route("/projets", methods=["GET", "POST"])
@login_required
@rh_requis
def projets():
    return _gerer_contexte(Projet, "parametres/contextes.html", "Projets", avec_projet=True)


# --- Grille KPI --------------------------------------------------------------
@bp.route("/criteres", methods=["GET", "POST"])
@login_required
@rh_requis
def criteres():
    erreurs, donnees, edition = {}, {}, None
    if request.method == "POST":
        action = request.form.get("action", "enregistrer")
        c = db.session.get(Critere, request.form.get("id", type=int) or 0)
        if action == "basculer" and c:
            c.actif = not c.actif
            journaliser("critere_statut", c.libelle, "actif" if c.actif else "inactif")
            db.session.commit()
            flash(f"Critère « {c.libelle} » {'activé' if c.actif else 'désactivé'}. "
                  "Les évaluations déjà soumises ne changent pas.", "succes")
            return redirect(request.path)
        donnees = {k: (request.form.get(k) or "").strip() for k in ("libelle", "description", "poids", "ordre")}
        if not donnees["libelle"]:
            erreurs["libelle"] = "Le libellé est obligatoire."
        elif db.session.scalar(select(Critere.id).where(func.lower(Critere.libelle) == donnees["libelle"].lower(),
                                                        Critere.id != (c.id if c else -1))):
            erreurs["libelle"] = "Ce critère existe déjà."
        try:
            poids = int(donnees["poids"] or 1)
            if not 1 <= poids <= 5:
                raise ValueError
        except ValueError:
            erreurs["poids"] = "Le poids doit être un entier de 1 à 5."
            poids = 1
        try:
            ordre = int(donnees["ordre"] or 0)
        except ValueError:
            erreurs["ordre"] = "Ordre invalide."
            ordre = 0
        if not erreurs:
            nouveau = c is None
            c = c or Critere()
            c.libelle, c.description, c.poids, c.ordre = donnees["libelle"], donnees["description"] or None, poids, ordre
            if nouveau:
                db.session.add(c)
            journaliser("critere_" + ("cree" if nouveau else "modifie"), c.libelle, f"poids={poids}")
            db.session.commit()
            flash("Critère enregistré. Le nouveau poids s'applique aux évaluations à venir.", "succes")
            return redirect(request.path)
        edition = c
    elif request.args.get("modifier", type=int):
        edition = db.session.get(Critere, request.args.get("modifier", type=int)) or abort(404)
        donnees = {"libelle": edition.libelle, "description": edition.description or "",
                   "poids": str(edition.poids), "ordre": str(edition.ordre)}
    elements = db.session.scalars(select(Critere).order_by(Critere.actif.desc(), Critere.ordre, Critere.id)).all()
    utilises = set(db.session.scalars(select(EvaluationNote.critere_id).distinct()))
    total_poids = sum(c.poids for c in elements if c.actif)
    return render_template("parametres/criteres.html", elements=elements, edition=edition, donnees=donnees,
                           erreurs=erreurs, utilises=utilises, total_poids=total_poids)


# --- Comptes utilisateurs ------------------------------------------------------
@bp.route("/utilisateurs", methods=["GET", "POST"])
@login_required
@rh_requis
def utilisateurs():
    erreurs, donnees, identifiants = {}, {}, []
    if request.method == "POST":
        action = request.form.get("action")
        if action == "creer":
            donnees = {k: (request.form.get(k) or "").strip() for k in ("email", "role", "employe_id")}
            email = donnees["email"].lower()
            if not email or "@" not in email:
                erreurs["email"] = "Adresse e-mail invalide."
            elif db.session.scalar(select(Utilisateur.id).where(Utilisateur.email == email)):
                erreurs["email"] = "Un compte existe déjà avec cette adresse."
            if donnees["role"] not in Role.TOUS:
                erreurs["role"] = "Rôle invalide."
            emp_id = int(donnees["employe_id"]) if donnees["employe_id"] else None
            if donnees["role"] == Role.COLLABORATEUR and not emp_id:
                erreurs["employe_id"] = "Un collaborateur doit être lié à sa fiche salarié."
            if emp_id and db.session.scalar(select(Utilisateur.id).where(Utilisateur.employe_id == emp_id)):
                erreurs["employe_id"] = "Ce salarié a déjà un compte."
            if not erreurs:
                mdp = mot_de_passe_temporaire()
                u = Utilisateur(email=email, role=donnees["role"], employe_id=emp_id, doit_changer_mdp=True)
                u.set_password(mdp)
                db.session.add(u)
                journaliser("compte_cree", email, Role.LIBELLES[u.role])
                db.session.commit()
                identifiants = [(u, mdp)]
                donnees = {}
        elif action == "creer_evaluateurs":
            # Crée un compte pour chaque N+1 / responsable qui n'en a pas et dispose d'un e-mail
            ids = set(db.session.scalars(select(Affectation.evaluateur_id).where(
                Affectation.actif.is_(True), Affectation.evaluateur_id.isnot(None))))
            ids |= set(db.session.scalars(select(Departement.responsable_id).where(Departement.responsable_id.isnot(None))))
            ids |= set(db.session.scalars(select(Projet.responsable_id).where(Projet.responsable_id.isnot(None))))
            avec_compte = set(db.session.scalars(select(Utilisateur.employe_id).where(Utilisateur.employe_id.isnot(None))))
            sans_email = 0
            for e in db.session.scalars(select(Employe).where(Employe.id.in_(ids - avec_compte), Employe.actif.is_(True))):
                if not e.email or db.session.scalar(select(Utilisateur.id).where(Utilisateur.email == e.email.lower())):
                    sans_email += 1
                    continue
                mdp = mot_de_passe_temporaire()
                u = Utilisateur(email=e.email.lower(), role=Role.COLLABORATEUR, employe_id=e.id, doit_changer_mdp=True)
                u.set_password(mdp)
                db.session.add(u)
                identifiants.append((u, mdp))
            journaliser("comptes_evaluateurs_crees", f"{len(identifiants)} comptes")
            db.session.commit()
            if sans_email:
                flash(f"{sans_email} évaluateur(s) sans e-mail (ou e-mail déjà utilisé) : complétez leur fiche.", "info")
            if not identifiants and not sans_email:
                flash("Tous les évaluateurs ont déjà un compte.", "info")
        else:
            u = db.session.get(Utilisateur, request.form.get("id", type=int) or 0) or abort(404)
            if u.id == current_user.id and action in ("basculer", "role"):
                flash("Vous ne pouvez pas modifier votre propre statut ou rôle.", "erreur")
                return redirect(request.path)
            if action == "basculer":
                u.actif = not u.actif
                journaliser("compte_" + ("active" if u.actif else "desactive"), u.email)
                flash(f"Compte {u.email} {'activé' if u.actif else 'désactivé'}.", "succes")
            elif action == "role":
                role = request.form.get("role")
                if role in Role.TOUS:
                    u.role = role
                    journaliser("compte_role", u.email, Role.LIBELLES[role])
                    flash(f"Rôle de {u.email} : {Role.LIBELLES[role]}.", "succes")
            elif action == "reinitialiser":
                mdp = mot_de_passe_temporaire()
                u.set_password(mdp)
                u.doit_changer_mdp = True
                u.echecs_connexion, u.bloque_jusqua = 0, None
                journaliser("compte_mdp_reinitialise", u.email)
                identifiants = [(u, mdp)]
            db.session.commit()
            if not identifiants:
                return redirect(request.path)

    comptes = db.session.scalars(select(Utilisateur).options(joinedload(Utilisateur.employe))
                                 .order_by(Utilisateur.actif.desc(), Utilisateur.role, Utilisateur.email)).all()
    sans_compte = db.session.scalars(
        select(Employe).where(Employe.actif.is_(True), ~Employe.id.in_(
            select(Utilisateur.employe_id).where(Utilisateur.employe_id.isnot(None))))
        .order_by(Employe.nom)).all()
    return render_template("parametres/utilisateurs.html", comptes=comptes, sans_compte=sans_compte,
                           roles=Role.LIBELLES, erreurs=erreurs, donnees=donnees, identifiants=identifiants)
