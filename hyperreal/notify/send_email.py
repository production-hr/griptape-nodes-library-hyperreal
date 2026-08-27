from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from griptape_nodes.exe_types.core_types import Parameter, ParameterMode
from griptape_nodes.exe_types.node_types import AsyncResult, SuccessFailureNode
from griptape_nodes.retained_mode.griptape_nodes import GriptapeNodes

logger = logging.getLogger("griptape_nodes")

SECRET_HOST = "SMTP_HOST"
SECRET_PORT = "SMTP_PORT"
SECRET_USER = "SMTP_USER"
SECRET_PASSWORD = "SMTP_PASSWORD"
SMTP_TIMEOUT_SECONDS = 30


class SendEmail(SuccessFailureNode):
    """Send a plain-text email over SMTP (Google Workspace app password, or any SMTP host).

    Standard library only. Secrets: SMTP_HOST, SMTP_PORT (587 = STARTTLS, 465 = SSL),
    SMTP_USER, SMTP_PASSWORD. Built for pipeline notifications — "training launched",
    "render finished" — not for bulk mail.
    """

    def __init__(self, name: str, metadata: dict[Any, Any] | None = None, **_: Any) -> None:
        node_metadata = {
            "category": "notify/email",
            "description": "Send a plain-text notification email over SMTP.",
        }
        if metadata:
            node_metadata.update(metadata)
        super().__init__(name, metadata=node_metadata)

        self.add_parameter(
            Parameter(
                name="to",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Recipient address(es), comma-separated.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="subject",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Email subject.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="body",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Plain-text body. Wire a node's summary output here.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
                ui_options={"multiline": True},
            )
        )
        self.add_parameter(
            Parameter(
                name="from_address",
                input_types=["str"],
                type="str",
                default_value="",
                tooltip="Sender address. Empty = SMTP_USER.",
                allowed_modes={ParameterMode.INPUT, ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="enabled",
                type="bool",
                default_value=True,
                tooltip="Off = skip sending (succeeds without emailing). Handy for dry runs.",
                allowed_modes={ParameterMode.PROPERTY},
            )
        )
        self.add_parameter(
            Parameter(
                name="sent",
                output_type="bool",
                tooltip="True when the SMTP server accepted the message.",
                allowed_modes={ParameterMode.OUTPUT},
            )
        )
        self._create_status_parameters(
            result_details_tooltip="Details about the email send",
            result_details_placeholder="Send details will appear here.",
        )

    def validate_before_node_run(self) -> list[Exception] | None:
        missing = self._missing_secrets()
        if missing:
            return [
                ValueError(
                    f"{', '.join(missing)} not set. Add them under Settings > API Keys & Secrets "
                    "(for Google Workspace: smtp.gmail.com, 587, the sending address, an app password), "
                    "then restart the engine."
                )
            ]
        return None

    def process(self) -> AsyncResult[None]:
        self._clear_execution_status()
        yield lambda: self._process()

    def _process(self) -> None:
        try:
            if not bool(self.parameter_values.get("enabled", True)):
                self.parameter_output_values["sent"] = False
                self._set_status_results(was_successful=True, result_details="Email disabled — nothing sent.")
                return
            missing = self._missing_secrets()
            if missing:
                raise ValueError(f"{', '.join(missing)} not set.")
            secrets = GriptapeNodes.SecretsManager()
            host = str(secrets.get_secret(SECRET_HOST)).strip()
            port = int(str(secrets.get_secret(SECRET_PORT)).strip() or 587)
            user = str(secrets.get_secret(SECRET_USER)).strip()
            password = str(secrets.get_secret(SECRET_PASSWORD))

            recipients = [a.strip() for a in str(self.parameter_values.get("to") or "").split(",") if a.strip()]
            if not recipients:
                raise ValueError("No recipient — set 'to' (wire notify_to from the settings node).")
            sender = (self.parameter_values.get("from_address") or "").strip() or user
            subject = (self.parameter_values.get("subject") or "").strip() or "(no subject)"
            body = self.parameter_values.get("body") or ""

            msg = EmailMessage()
            msg["From"] = sender
            msg["To"] = ", ".join(recipients)
            msg["Subject"] = subject
            msg.set_content(body)

            if port == 465:
                with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
                    smtp.login(user, password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.ehlo()
                    smtp.login(user, password)
                    smtp.send_message(msg)

            self.parameter_output_values["sent"] = True
            self._set_status_results(
                was_successful=True,
                result_details=f"Sent '{subject}' to {', '.join(recipients)} via {host}:{port} as {sender}.",
            )
        except smtplib.SMTPAuthenticationError as e:
            self.parameter_output_values["sent"] = False
            msg = (
                f"SMTP login rejected for {SECRET_USER} ({e.smtp_code}). For Google Workspace use a 16-character "
                "APP PASSWORD (2-Step Verification must be on), not the account password."
            )
            self._set_status_results(was_successful=False, result_details=msg)
            self._handle_failure_exception(RuntimeError(msg))
        except Exception as e:
            self.parameter_output_values["sent"] = False
            self._set_status_results(was_successful=False, result_details=str(e))
            self._handle_failure_exception(e)

    def _missing_secrets(self) -> list[str]:
        secrets = GriptapeNodes.SecretsManager()
        missing = []
        for name in (SECRET_HOST, SECRET_PORT, SECRET_USER, SECRET_PASSWORD):
            try:
                value = secrets.get_secret(name)
            except Exception:
                value = None
            if not value:
                missing.append(name)
        return missing
