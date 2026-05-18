"""Mail Service - SMTP email sender for booking notifications"""

import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from datetime import datetime
from app.config import settings

logger = logging.getLogger(__name__)


class MailService:
    """Service for sending booking-related emails via SMTP"""
    
    def __init__(
        self,
        smtp_host: str = settings.SMTP_HOST,
        smtp_port: int = settings.SMTP_PORT,
        smtp_user: str = settings.SMTP_USER,
        smtp_password: str = settings.SMTP_PASSWORD,
        use_tls: bool = settings.SMTP_USE_TLS,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.use_tls = use_tls
    
    def _send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: Optional[str] = None,
    ) -> bool:
        """
        Send email via SMTP.
        
        Args:
            to_email: Recipient email address
            subject: Email subject
            html_content: HTML formatted email body
            text_content: Plain text fallback body
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Create message
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.smtp_user
            msg["To"] = to_email
            
            # Attach text and HTML parts
            if text_content:
                msg.attach(MIMEText(text_content, "plain"))
            msg.attach(MIMEText(html_content, "html"))
            
            # Send via SMTP
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)
            
            logger.info(f"✓ Email sent to {to_email} — {subject}")
            return True
            
        except Exception as e:
            logger.error(f"✗ Failed to send email to {to_email}: {str(e)}")
            return False
    
    def send_booking_confirmation(
        self,
        customer_email: str,
        customer_name: str,
        business_name: str,
        datetime_str: str,
        party_size: int,
        booking_id: str,
        special_requests: Optional[str] = None,
    ) -> bool:
        """Send booking confirmation email"""
        
        subject = f"✓ Booking Confirmed — {business_name}"
        
        text_content = f"""
Booking Confirmation

Hello {customer_name},

Your booking with {business_name} has been confirmed.

Booking Details:
- Date & Time: {datetime_str}
- Party Size: {party_size} person{'s' if party_size != 1 else ''}
- Booking ID: {booking_id}
{f'- Special Requests: {special_requests}' if special_requests else ''}

Thank you for booking with us!

Best regards,
{business_name}
        """
        
        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background-color: #f9f9f9; }}
        .header {{ background-color: #2c3e50; color: white; padding: 20px; border-radius: 5px; text-align: center; }}
        .content {{ background-color: white; padding: 20px; margin-top: 20px; border-radius: 5px; }}
        .details {{ margin: 20px 0; padding: 15px; background-color: #ecf0f1; border-left: 4px solid #27ae60; }}
        .detail-row {{ margin: 10px 0; }}
        .label {{ font-weight: bold; color: #2c3e50; }}
        .value {{ color: #555; }}
        .footer {{ text-align: center; margin-top: 30px; color: #999; font-size: 12px; }}
        .checkmark {{ color: #27ae60; font-weight: bold; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1><span class="checkmark">✓</span> Booking Confirmed</h1>
        </div>
        <div class="content">
            <p>Hello <strong>{customer_name}</strong>,</p>
            <p>Your booking with <strong>{business_name}</strong> has been confirmed.</p>
            
            <div class="details">
                <div class="detail-row">
                    <span class="label">📅 Date & Time:</span>
                    <span class="value">{datetime_str}</span>
                </div>
                <div class="detail-row">
                    <span class="label">👥 Party Size:</span>
                    <span class="value">{party_size} person{'s' if party_size != 1 else ''}</span>
                </div>
                <div class="detail-row">
                    <span class="label">🎫 Booking ID:</span>
                    <span class="value">{booking_id}</span>
                </div>
                {f'<div class="detail-row"><span class="label">📝 Special Requests:</span><span class="value">{special_requests}</span></div>' if special_requests else ''}
            </div>
            
            <p style="margin-top: 20px;">Thank you for booking with us! We look forward to seeing you.</p>
            <p>Best regards,<br><strong>{business_name}</strong></p>
        </div>
        <div class="footer">
            <p>This is an automated message. Please do not reply to this email.</p>
        </div>
    </div>
</body>
</html>
        """
        
        return self._send_email(customer_email, subject, html_content, text_content)
    
    def send_booking_rescheduled(
        self,
        customer_email: str,
        customer_name: str,
        business_name: str,
        old_datetime: str,
        new_datetime: str,
        party_size: int,
        booking_id: str,
        special_requests: Optional[str] = None,
    ) -> bool:
        """Send booking rescheduled notification email"""
        
        subject = f"📅 Booking Rescheduled — {business_name}"
        
        text_content = f"""
Booking Rescheduled

Hello {customer_name},

Your booking with {business_name} has been rescheduled.

Original Date & Time: {old_datetime}
New Date & Time: {new_datetime}

Booking Details:
- Party Size: {party_size} person{'s' if party_size != 1 else ''}
- Booking ID: {booking_id}
{f'- Special Requests: {special_requests}' if special_requests else ''}

Thank you for updating your booking!

Best regards,
{business_name}
        """
        
        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background-color: #f9f9f9; }}
        .header {{ background-color: #f39c12; color: white; padding: 20px; border-radius: 5px; text-align: center; }}
        .content {{ background-color: white; padding: 20px; margin-top: 20px; border-radius: 5px; }}
        .details {{ margin: 20px 0; padding: 15px; background-color: #ecf0f1; border-left: 4px solid #f39c12; }}
        .detail-row {{ margin: 10px 0; }}
        .label {{ font-weight: bold; color: #2c3e50; }}
        .value {{ color: #555; }}
        .comparison {{ display: flex; justify-content: space-between; align-items: center; margin: 15px 0; padding: 10px; background-color: #fff9e6; }}
        .old-time {{ color: #e74c3c; text-decoration: line-through; }}
        .arrow {{ margin: 0 10px; color: #f39c12; font-weight: bold; }}
        .new-time {{ color: #27ae60; font-weight: bold; }}
        .footer {{ text-align: center; margin-top: 30px; color: #999; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📅 Booking Rescheduled</h1>
        </div>
        <div class="content">
            <p>Hello <strong>{customer_name}</strong>,</p>
            <p>Your booking with <strong>{business_name}</strong> has been rescheduled.</p>
            
            <div class="comparison">
                <div class="old-time">{old_datetime}</div>
                <div class="arrow">→</div>
                <div class="new-time">{new_datetime}</div>
            </div>
            
            <div class="details">
                <div class="detail-row">
                    <span class="label">👥 Party Size:</span>
                    <span class="value">{party_size} person{'s' if party_size != 1 else ''}</span>
                </div>
                <div class="detail-row">
                    <span class="label">🎫 Booking ID:</span>
                    <span class="value">{booking_id}</span>
                </div>
                {f'<div class="detail-row"><span class="label">📝 Special Requests:</span><span class="value">{special_requests}</span></div>' if special_requests else ''}
            </div>
            
            <p style="margin-top: 20px;">Thank you for updating your booking!</p>
            <p>Best regards,<br><strong>{business_name}</strong></p>
        </div>
        <div class="footer">
            <p>This is an automated message. Please do not reply to this email.</p>
        </div>
    </div>
</body>
</html>
        """
        
        return self._send_email(customer_email, subject, html_content, text_content)
    
    def send_booking_cancelled(
        self,
        customer_email: str,
        customer_name: str,
        business_name: str,
        datetime_str: str,
        party_size: int,
        booking_id: str,
        reason: Optional[str] = None,
    ) -> bool:
        """Send booking cancellation notification email"""
        
        subject = f"❌ Booking Cancelled — {business_name}"
        
        text_content = f"""
Booking Cancelled

Hello {customer_name},

Your booking with {business_name} has been cancelled.

Booking Details:
- Date & Time: {datetime_str}
- Party Size: {party_size} person{'s' if party_size != 1 else ''}
- Booking ID: {booking_id}
{f'- Reason: {reason}' if reason else ''}

If you would like to reschedule or have any questions, please contact us.

Best regards,
{business_name}
        """
        
        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; background-color: #f9f9f9; }}
        .header {{ background-color: #e74c3c; color: white; padding: 20px; border-radius: 5px; text-align: center; }}
        .content {{ background-color: white; padding: 20px; margin-top: 20px; border-radius: 5px; }}
        .details {{ margin: 20px 0; padding: 15px; background-color: #ecf0f1; border-left: 4px solid #e74c3c; }}
        .detail-row {{ margin: 10px 0; }}
        .label {{ font-weight: bold; color: #2c3e50; }}
        .value {{ color: #555; }}
        .footer {{ text-align: center; margin-top: 30px; color: #999; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>❌ Booking Cancelled</h1>
        </div>
        <div class="content">
            <p>Hello <strong>{customer_name}</strong>,</p>
            <p>Your booking with <strong>{business_name}</strong> has been cancelled.</p>
            
            <div class="details">
                <div class="detail-row">
                    <span class="label">📅 Date & Time:</span>
                    <span class="value">{datetime_str}</span>
                </div>
                <div class="detail-row">
                    <span class="label">👥 Party Size:</span>
                    <span class="value">{party_size} person{'s' if party_size != 1 else ''}</span>
                </div>
                <div class="detail-row">
                    <span class="label">🎫 Booking ID:</span>
                    <span class="value">{booking_id}</span>
                </div>
                {f'<div class="detail-row"><span class="label">📝 Reason:</span><span class="value">{reason}</span></div>' if reason else ''}
            </div>
            
            <p style="margin-top: 20px;">If you would like to reschedule or have any questions, please contact us.</p>
            <p>Best regards,<br><strong>{business_name}</strong></p>
        </div>
        <div class="footer">
            <p>This is an automated message. Please do not reply to this email.</p>
        </div>
    </div>
</body>
</html>
        """
        
        return self._send_email(customer_email, subject, html_content, text_content)


# Global instance
mail_service = MailService()
