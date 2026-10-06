-- Supabase only setup. Run once on the project (SQL editor, or the migration that was applied for
-- you) AFTER app/schema.sql. Safe to run again.

-- 1. Every Supabase Auth user has a profile, and deleting the auth user deletes the profile.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'profiles_id_fkey') THEN
        ALTER TABLE public.profiles
            ADD CONSTRAINT profiles_id_fkey FOREIGN KEY (id) REFERENCES auth.users (id) ON DELETE CASCADE;
    END IF;
END $$;

-- 2. A new sign up gets a "pending" profile. It cannot use the API until an admin approves it.
CREATE OR REPLACE FUNCTION public.handle_new_user() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
BEGIN
    INSERT INTO public.profiles (id, email) VALUES (NEW.id, COALESCE(NEW.email, ''))
    ON CONFLICT (id) DO NOTHING;
    RETURN NEW;
END;
$$;
REVOKE EXECUTE ON FUNCTION public.handle_new_user() FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- Accounts that signed up before this ran.
INSERT INTO public.profiles (id, email)
SELECT id, COALESCE(email, '') FROM auth.users
ON CONFLICT (id) DO NOTHING;

-- 3. Supabase exposes the public schema through a REST API. These tables are for the backend only.
REVOKE ALL ON public.profiles, public.applicants, public.screening_results,
              public.evidence_files, public.nacta_list, public.audit_log FROM anon, authenticated;

-- 4. The first admin. Replace the address with the one you signed up with, and run it by hand:
--      UPDATE public.profiles SET status = 'approved', role = 'admin' WHERE email = 'you@example.com';
