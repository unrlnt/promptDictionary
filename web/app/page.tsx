import { redirect } from "next/navigation";

// The app entry just routes into the protected area; /dashboard sends
// unauthenticated visitors to /login.
export default function Home() {
  redirect("/dashboard");
}
