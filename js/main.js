/**
 * Riverview Fire Department - Main JavaScript
 * Shared functionality across all pages
 */

// Mobile menu toggle
function toggleMenu() {
    const nav = document.getElementById('mainNav');
    if (nav) {
        nav.classList.toggle('active');
    }
}

// Close mobile menu
function closeMenu() {
    const nav = document.getElementById('mainNav');
    if (nav) {
        nav.classList.remove('active');
    }
}

// Close menu when clicking outside
document.addEventListener('click', function(event) {
    const nav = document.getElementById('mainNav');
    const toggle = document.querySelector('.mobile-menu-toggle');

    if (nav && toggle) {
        const isClickInsideNav = nav.contains(event.target);
        const isClickOnToggle = toggle.contains(event.target);

        if (!isClickInsideNav && !isClickOnToggle && nav.classList.contains('active')) {
            nav.classList.remove('active');
        }
    }
});
