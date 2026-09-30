"""Control Panel domain: global settings singleton and emergency controls."""

from app.domain.control_panel.errors import ControlPanelDomainError
from app.domain.control_panel.manager import ControlPanelManager

__all__ = ["ControlPanelDomainError", "ControlPanelManager"]
