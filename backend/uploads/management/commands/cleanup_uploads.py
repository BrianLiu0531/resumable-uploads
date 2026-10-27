from django.core.management.base import BaseCommand

from uploads.util import cleanup_expired


class Command(BaseCommand):
    help = "Delete expired uploads and their stored bytes"

    def handle(self, *args, **options):
        self.stdout.write(f"removed {cleanup_expired()} expired uploads")
