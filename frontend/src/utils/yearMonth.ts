// Month/Year handling for the profile's education & experience date pickers. Dates are
// stored as "YYYY-MM" (or "YYYY" when only the year is known). Ported from the classic
// dashboard's parseYM so existing/imported values in varied formats still populate.

const MONTH_IDX: Record<string, string> = {
  jan: "01", feb: "02", mar: "03", apr: "04", may: "05", jun: "06",
  jul: "07", aug: "08", sep: "09", oct: "10", nov: "11", dec: "12",
};

export const MONTHS: [string, string][] = [
  ["01", "January"], ["02", "February"], ["03", "March"], ["04", "April"],
  ["05", "May"], ["06", "June"], ["07", "July"], ["08", "August"],
  ["09", "September"], ["10", "October"], ["11", "November"], ["12", "December"],
];

/** Parse a stored/extracted date into {year, month("01".."12")}. Handles "2020-09",
 *  "2020/9", "09/2020", "Jun 2020", "September 2020", "2019"; blank/"Present" → empty. */
export function parseYM(value: string | null | undefined): { year: string; month: string } {
  const s = (value || "").trim();
  let m: RegExpMatchArray | null;
  if ((m = s.match(/^(\d{4})[-/.](\d{1,2})$/))) return { year: m[1], month: m[2].padStart(2, "0") };
  if ((m = s.match(/^(\d{1,2})[-/](\d{4})$/))) return { year: m[2], month: m[1].padStart(2, "0") };
  if ((m = s.match(/^([A-Za-z]{3,})\.?\s+(\d{4})$/))) return { year: m[2], month: MONTH_IDX[m[1].slice(0, 3).toLowerCase()] || "" };
  if ((m = s.match(/^(\d{4})$/))) return { year: m[1], month: "" };
  return { year: "", month: "" };
}

/** The year dropdown range: now+6 down to 1965 (matches the classic picker). */
export function yearRange(): number[] {
  const now = new Date().getFullYear();
  const out: number[] = [];
  for (let y = now + 6; y >= 1965; y--) out.push(y);
  return out;
}

/** Combine a year + month back into the stored string ("YYYY-MM", "YYYY", or ""). */
export function combineYM(year: string, month: string): string {
  return year ? (month ? `${year}-${month}` : year) : "";
}
