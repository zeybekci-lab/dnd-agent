import { redirect } from 'next/navigation';

// The old single-player page is retired. Solo play now runs on the full table engine — open the
// table and tap "Play solo — one character", which spins up a one-PC game with the dice menu,
// turn order, reactions, death saves, rewind/redo, concentration, and hit dice all included.
export default function PlayPage() {
    redirect('/table');
}
