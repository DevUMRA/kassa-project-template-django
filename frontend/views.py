from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie


@login_required
@ensure_csrf_cookie
def app(request):
    """Кассаның бір беттік қолданбасы. Деректерді /api/ арқылы сессиямен (CSRF тақырыбымен) алады."""
    return render(request, 'frontend/app.html')
