"""
Authentication Service: Login, Token Management, User CRUD
"""
from datetime import datetime, timezone
from typing import Optional, List

from sqlalchemy.orm import Session
from loguru import logger

from app.models.rbac import User, UserRole, Role, Permission, RolePermission
from app.security.password import hash_password, verify_password
from app.security.jwt_handler import (
    create_access_token, create_refresh_token, verify_refresh_token
)
from app.schemas.auth import (
    LoginRequest, TokenResponse, UserCreate, UserUpdate, UserResponse
)
from app.audit.service import AuditService
from app.core.config import get_settings

settings = get_settings()


class AuthService:
    """Handles authentication and user management."""

    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditService(db)

    # ========================================================================
    # Authentication
    # ========================================================================

    def authenticate(self, login: LoginRequest, ip_address: str = None) -> TokenResponse:
        """Authenticate user and return tokens."""
        user = self.db.query(User).filter(User.username == login.username).first()

        if not user:
            raise ValueError("Invalid credentials")

        if user.is_locked:
            raise PermissionError("Account is locked. Contact admin.")

        if not user.is_active:
            raise PermissionError("Account is inactive.")

        if not verify_password(login.password, user.password_hash):
            # Increment failed attempts
            user.failed_attempts = (user.failed_attempts or 0) + 1
            if user.failed_attempts >= settings.MAX_LOGIN_ATTEMPTS:
                user.is_locked = True
                logger.warning(f"Account locked: {user.username}")
            self.db.commit()
            raise ValueError("Invalid credentials")

        # Reset failed attempts on success
        user.failed_attempts = 0
        user.last_login = datetime.now(timezone.utc)
        self.db.commit()

        # Build token payload
        token_data = {
            "sub": user.username,
            "user_id": user.id,
            "roles": user.role_codes,
            "permissions": list(user.permissions),
        }

        access_token = create_access_token(token_data)
        refresh_token = create_refresh_token({"sub": user.username, "user_id": user.id})

        # Audit login
        self.audit.log(
            table_name="auth",
            action_type="LOGIN",
            changed_by=user.username,
            ip_address=ip_address,
            notes="Successful login",
        )
        self.db.commit()

        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user=self._to_user_response(user),
        )

    def refresh_tokens(self, refresh_token_str: str) -> TokenResponse:
        """Refresh access token using refresh token."""
        payload = verify_refresh_token(refresh_token_str)
        if not payload:
            raise ValueError("Invalid refresh token")

        user = self.db.query(User).filter(
            User.username == payload["sub"], User.is_active == True
        ).first()

        if not user:
            raise ValueError("User not found")

        token_data = {
            "sub": user.username,
            "user_id": user.id,
            "roles": user.role_codes,
            "permissions": list(user.permissions),
        }

        return TokenResponse(
            access_token=create_access_token(token_data),
            refresh_token=create_refresh_token({"sub": user.username, "user_id": user.id}),
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user=self._to_user_response(user),
        )

    def change_password(self, user: User, current_password: str, new_password: str):
        """Change user password."""
        if not verify_password(current_password, user.password_hash):
            raise ValueError("Current password is incorrect")

        user.password_hash = hash_password(new_password)
        user.password_changed_at = datetime.now(timezone.utc)
        self.db.commit()

        self.audit.log(
            table_name="auth",
            action_type="PASSWORD_CHANGE",
            changed_by=user.username,
        )
        self.db.commit()

    # ========================================================================
    # User CRUD
    # ========================================================================

    def create_user(self, data: UserCreate, created_by: str) -> UserResponse:
        """Create a new user."""
        # Check uniqueness
        if self.db.query(User).filter(User.username == data.username).first():
            raise ValueError(f"Username '{data.username}' already exists")
        if self.db.query(User).filter(User.mobile_no == data.mobile_no).first():
            raise ValueError(f"Mobile number '{data.mobile_no}' already exists")

        user = User(
            username=data.username,
            email=data.email,
            mobile_no=data.mobile_no,
            password_hash=hash_password(data.password),
            full_name=data.full_name,
            employee_code=data.employee_code,
            phone=data.phone,
            created_by=created_by,
        )
        self.db.add(user)
        self.db.flush()

        # Assign roles
        if data.role_ids:
            for role_id in data.role_ids:
                role = self.db.query(Role).filter(Role.id == role_id).first()
                if role:
                    ur = UserRole(user_id=user.id, role_id=role.id, assigned_by=created_by)
                    self.db.add(ur)

        self.db.flush()

        self.audit.log_insert(
            table_name="rbac_users",
            changed_by=created_by,
            record_pk=str(user.id),
            new_data={"username": user.username, "mobile_no": user.mobile_no, "full_name": user.full_name},
        )
        self.db.commit()
        self.db.refresh(user)

        return self._to_user_response(user)

    def update_user(self, user_id: int, data: UserUpdate, updated_by: str) -> UserResponse:
        """Update user details."""
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("User not found")

        old_data = {"email": user.email, "mobile_no": user.mobile_no, "full_name": user.full_name, "is_active": user.is_active}
        changed = []

        if data.email is not None and data.email != user.email:
            user.email = data.email
            changed.append("email")
        if data.mobile_no is not None and data.mobile_no != user.mobile_no:
            # Check uniqueness
            existing = self.db.query(User).filter(User.mobile_no == data.mobile_no, User.id != user_id).first()
            if existing:
                raise ValueError(f"Mobile number '{data.mobile_no}' already exists")
            user.mobile_no = data.mobile_no
            changed.append("mobile_no")
        if data.full_name is not None and data.full_name != user.full_name:
            user.full_name = data.full_name
            changed.append("full_name")
        if data.employee_code is not None:
            user.employee_code = data.employee_code
            changed.append("employee_code")
        if data.phone is not None:
            user.phone = data.phone
            changed.append("phone")
        if data.is_active is not None and data.is_active != user.is_active:
            user.is_active = data.is_active
            changed.append("is_active")

        # Update roles if provided
        if data.role_ids is not None:
            # Deactivate existing roles
            for ur in user.user_roles:
                ur.is_active = False
            # Add new roles
            for role_id in data.role_ids:
                existing = self.db.query(UserRole).filter(
                    UserRole.user_id == user.id, UserRole.role_id == role_id
                ).first()
                if existing:
                    existing.is_active = True
                else:
                    self.db.add(UserRole(user_id=user.id, role_id=role_id, assigned_by=updated_by))
            changed.append("roles")

        # Reset password if provided (admin action)
        if data.password and data.password.strip():
            user.password_hash = hash_password(data.password.strip())
            changed.append("password")

        user.updated_at = datetime.now(timezone.utc)

        if changed:
            self.audit.log_update(
                table_name="rbac_users",
                changed_by=updated_by,
                record_pk=str(user.id),
                old_data=old_data,
                new_data={"email": user.email, "mobile_no": user.mobile_no, "full_name": user.full_name, "is_active": user.is_active},
                changed_columns=changed,
            )

        self.db.commit()
        self.db.refresh(user)
        return self._to_user_response(user)

    def get_user(self, user_id: int) -> Optional[UserResponse]:
        """Get user by ID."""
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            return None
        return self._to_user_response(user)

    def list_users(self, page: int = 1, page_size: int = 50, search: str = None) -> dict:
        """List users with pagination."""
        query = self.db.query(User)
        if search:
            query = query.filter(
                (User.username.ilike(f"%{search}%")) |
                (User.full_name.ilike(f"%{search}%")) |
                (User.mobile_no.ilike(f"%{search}%")) |
                (User.email.ilike(f"%{search}%"))
            )

        total = query.count()
        users = query.order_by(User.id).offset((page - 1) * page_size).limit(page_size).all()

        return {
            "users": [self._to_user_response(u) for u in users],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def unlock_user(self, user_id: int, unlocked_by: str):
        """Unlock a locked user account."""
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError("User not found")
        user.is_locked = False
        user.failed_attempts = 0
        self.db.commit()

        self.audit.log(
            table_name="rbac_users",
            action_type="UPDATE",
            changed_by=unlocked_by,
            record_primary_key=str(user.id),
            notes="Account unlocked",
        )
        self.db.commit()

    # ========================================================================
    # Helpers
    # ========================================================================

    def _to_user_response(self, user: User) -> UserResponse:
        return UserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            mobile_no=user.mobile_no,
            full_name=user.full_name,
            employee_code=user.employee_code,
            phone=user.phone,
            is_active=user.is_active,
            is_locked=user.is_locked,
            last_login=user.last_login,
            created_at=user.created_at,
            roles=user.role_codes,
            permissions=list(user.permissions),
        )


# ========================================================================
# Bootstrap: Create Super Admin on first run
# ========================================================================

def create_super_admin_if_needed(db: Session):
    """Create the initial super admin user if it doesn't exist."""
    existing = db.query(User).filter(User.username == settings.SUPER_ADMIN_USERNAME).first()
    if existing:
        # Always ensure superadmin is unlocked and password is current
        changed = False
        if existing.is_locked or existing.failed_attempts > 0:
            existing.is_locked = False
            existing.failed_attempts = 0
            changed = True
            logger.info("Super admin unlocked (was locked).")
        # Reset password to env var value on every startup
        existing.password_hash = hash_password(settings.SUPER_ADMIN_PASSWORD)
        changed = True
        if changed:
            db.commit()
            logger.info("Super admin password synced from env.")
        return

    logger.info("Creating super admin user...")

    user = User(
        username=settings.SUPER_ADMIN_USERNAME,
        email=settings.SUPER_ADMIN_EMAIL,
        mobile_no="0000000000",
        password_hash=hash_password(settings.SUPER_ADMIN_PASSWORD),
        full_name="Super Administrator",
        created_by="SYSTEM",
    )
    db.add(user)
    db.flush()

    # Assign SUPER_ADMIN role
    super_role = db.query(Role).filter(Role.role_code == "SUPER_ADMIN").first()
    if super_role:
        db.add(UserRole(user_id=user.id, role_id=super_role.id, assigned_by="SYSTEM"))

    db.commit()
    logger.info(f"Super admin created: {settings.SUPER_ADMIN_USERNAME}")


def seed_permissions_if_needed(db: Session):
    """Seed all module permissions on startup. Idempotent — skips existing."""
    ALL_PERMISSIONS = [
        # (name, code, module, action, resource)
        ("View Data Tables", "DATA_VIEW", "data", "READ", "*"),
        ("Use Data Editor", "DATA_EDITOR", "data", "UPDATE", "*"),
        ("View Jobs Dashboard", "JOBS_VIEW", "data", "READ", "jobs"),
        ("View MSA Stock", "MSA_VIEW", "data_prep", "READ", "msa"),
        ("Execute MSA Calculation", "MSA_EXECUTE", "data_prep", "CREATE", "msa"),
        ("View BDC Creation", "BDC_VIEW", "data_prep", "READ", "bdc"),
        ("Execute BDC Creation", "BDC_EXECUTE", "data_prep", "CREATE", "bdc"),
        ("View Grid Builder", "GRID_VIEW", "data_prep", "READ", "grid_builder"),
        ("Run Grid Builder", "GRID_RUN", "data_prep", "CREATE", "grid_builder"),
        ("Manage Grid Builder", "GRID_MANAGE", "data_prep", "UPDATE", "grid_builder"),
        ("View Lookup Art Master", "LOOKUP_VIEW", "data_prep", "READ", "lookup"),
        ("Manage Contrib Presets", "CONTRIB_PRESETS", "contribution", "UPDATE", "contrib_presets"),
        ("Manage Contrib Mappings", "CONTRIB_MAPPINGS", "contribution", "UPDATE", "contrib_mappings"),
        ("Execute Contribution", "CONTRIB_EXECUTE", "contribution", "CREATE", "contrib_execute"),
        ("Review Contribution", "CONTRIB_REVIEW", "contribution", "READ", "contrib_review"),
        ("View Trends Dashboard", "TRENDS_DASHBOARD", "trends", "READ", "trends"),
        ("Upload Trend Data", "TRENDS_UPLOAD", "trends", "CREATE", "trends"),
        ("Review Trend Data", "TRENDS_REVIEW", "trends", "READ", "trends"),
        ("View Pending Allocation Report", "REPORTS_PEND_ALC", "reports", "READ", "reports"),
        ("View Data Checklist", "CHECKLIST_VIEW", "validation", "READ", "checklist"),
        ("Manage Data Checklist", "CHECKLIST_MANAGE", "validation", "UPDATE", "checklist"),
        ("View Store SLOC Validation", "STORE_SLOC_VIEW", "validation", "READ", "store_sloc"),
    ]

    added = 0
    for name, code, module, action, resource in ALL_PERMISSIONS:
        exists = db.query(Permission).filter(Permission.permission_code == code).first()
        if not exists:
            db.add(Permission(
                permission_name=name, permission_code=code,
                module=module, action=action, resource=resource,
            ))
            added += 1

    if added:
        db.commit()
        logger.info(f"Seeded {added} new permissions")

        # Assign all new permissions to SUPER_ADMIN
        super_role = db.query(Role).filter(Role.role_code == "SUPER_ADMIN").first()
        if super_role:
            all_perms = db.query(Permission).filter(Permission.is_active == True).all()
            existing_perm_ids = {rp.permission_id for rp in
                db.query(RolePermission).filter(RolePermission.role_id == super_role.id).all()}
            for p in all_perms:
                if p.id not in existing_perm_ids:
                    db.add(RolePermission(role_id=super_role.id, permission_id=p.id, granted_by="SYSTEM"))
            db.commit()
            logger.info("Assigned new permissions to SUPER_ADMIN")
    else:
        logger.info("All permissions already seeded")
