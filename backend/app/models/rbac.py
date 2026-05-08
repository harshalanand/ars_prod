"""
RBAC Models: Roles, Permissions, Users, User-Roles
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from app.database.session import Base


class Role(Base):
    __tablename__ = "rbac_roles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    role_name = Column(String(100), nullable=False, unique=True)
    role_code = Column(String(50), nullable=False, unique=True)
    description = Column(String(500))
    is_system_role = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(String(100))

    # Relationships
    role_permissions = relationship("RolePermission", back_populates="role", lazy="selectin")
    user_roles = relationship("UserRole", back_populates="role", lazy="selectin")
    column_restrictions = relationship("ColumnRestriction", back_populates="role", lazy="selectin")


class Permission(Base):
    __tablename__ = "rbac_permissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    permission_name = Column(String(200), nullable=False)
    permission_code = Column(String(100), nullable=False, unique=True)
    module = Column(String(100), nullable=False)
    action = Column(String(50), nullable=False)
    resource = Column(String(200))
    description = Column(String(500))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    role_permissions = relationship("RolePermission", back_populates="permission", lazy="selectin")


class RolePermission(Base):
    __tablename__ = "rbac_role_permissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    role_id = Column(Integer, ForeignKey("rbac_roles.id"), nullable=False)
    permission_id = Column(Integer, ForeignKey("rbac_permissions.id"), nullable=False)
    granted_at = Column(DateTime, default=datetime.utcnow)
    granted_by = Column(String(100))

    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
    )

    # Relationships - use selectin for eager loading
    role = relationship("Role", back_populates="role_permissions")
    permission = relationship("Permission", back_populates="role_permissions", lazy="selectin")


class User(Base):
    __tablename__ = "rbac_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), nullable=False, unique=True, index=True)
    email = Column(String(200), unique=True, index=True)
    mobile_no = Column(String(15), nullable=False, unique=True, index=True)
    password_hash = Column(String(500), nullable=False)
    full_name = Column(String(200), nullable=False)
    employee_code = Column(String(50))
    phone = Column(String(20))
    is_active = Column(Boolean, default=True)
    is_locked = Column(Boolean, default=False)
    failed_attempts = Column(Integer, default=0)
    last_login = Column(DateTime)
    password_changed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(String(100))

    # Relationships
    user_roles = relationship("UserRole", back_populates="user", lazy="selectin")
    store_access = relationship("UserStoreAccess", back_populates="user", lazy="selectin")
    region_access = relationship("UserRegionAccess", back_populates="user", lazy="selectin")
    category_access = relationship("UserCategoryAccess", back_populates="user", lazy="selectin")

    @property
    def roles(self):
        return [ur.role for ur in self.user_roles if ur.is_active]

    @property
    def role_codes(self):
        return [ur.role.role_code for ur in self.user_roles if ur.is_active and ur.role]

    @property
    def permissions(self):
        perms = set()
        for ur in self.user_roles:
            if ur.is_active and ur.role:
                for rp in ur.role.role_permissions:
                    if rp.permission and rp.permission.is_active:
                        perms.add(rp.permission.permission_code)
        return perms


class UserRole(Base):
    __tablename__ = "rbac_user_roles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("rbac_users.id"), nullable=False)
    role_id = Column(Integer, ForeignKey("rbac_roles.id"), nullable=False)
    assigned_at = Column(DateTime, default=datetime.utcnow)
    assigned_by = Column(String(100))
    is_active = Column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_role"),
    )

    # Relationships - use selectin for eager loading
    user = relationship("User", back_populates="user_roles")
    role = relationship("Role", back_populates="user_roles", lazy="selectin")
