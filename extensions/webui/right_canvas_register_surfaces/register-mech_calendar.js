// Mech.Calendar Canvas Surface Registration
export default async function registerMechCalendarSurface(canvas) {
    canvas.registerSurface({
        id: 'mech_calendar',
        title: 'Calendar',
        icon: 'calendar_month',
        order: 55,
        modalPath: '/plugins/mech_calendar/webui/canvas.html?v=20260905a',
        actionOnly: false,
    });
    console.log('[Mech.Calendar] Registered as Canvas surface');
}
