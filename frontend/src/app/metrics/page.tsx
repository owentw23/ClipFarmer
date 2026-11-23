import { redirect } from "next/navigation";

// The metrics dashboard now lives on the home page.
export default function MetricsRedirect() {
  redirect("/");
}
