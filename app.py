import streamlit as st
import psycopg2
import pandas as pd
import bcrypt
import io
from reportlab.lib.pagesizes import letter, landscape
from reportlab.pdfgen import canvas

# 1. Conexión segura usando Secrets de Streamlit
def get_connection():
    return psycopg2.connect(st.secrets["DATABASE_URL"])

# 2. Utilidades de contraseñas
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

# 3. Configuración visual
st.set_page_config(page_title="Sistema de Mini Lecciones", layout="wide")

if "user" not in st.session_state:
    st.session_state.user = None

# 4. Pantalla de Autenticación
if not st.session_state.user:
    st.title("🎓 Sistema de Mini Lecciones")
    tab_login, tab_register = st.tabs(["Iniciar Sesión", "Registrarse"])
    
    with tab_login:
        email = st.text_input("Correo electrónico")
        pwd = st.text_input("Contraseña", type="password")
        if st.button("Entrar", use_container_width=True):
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute("SELECT id, name, password_hash, role FROM users WHERE email = %s", (email,))
                user = cur.fetchone()
                conn.close()
                if user and check_password(pwd, user[2]):
                    st.session_state.user = {"id": user[0], "name": user[1], "role": user[3]}
                    st.rerun()
                else:
                    st.error("Correo o contraseña incorrectos")
            except Exception as e:
                st.error(f"Error de conexión: {e}")
                
    with tab_register:
        reg_name = st.text_input("Nombre completo")
        reg_email = st.text_input("Correo para registro")
        reg_pwd = st.text_input("Contraseña nueva", type="password")
        if st.button("Crear Cuenta", use_container_width=True):
            try:
                conn = get_connection()
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO users (name, email, password_hash) VALUES (%s, %s, %s)",
                    (reg_name, reg_email, hash_password(reg_pwd))
                )
                conn.commit()
                conn.close()
                st.success("¡Cuenta creada con éxito! Ahora puedes iniciar sesión.")
            except Exception as e:
                st.error(f"Error al registrar: {e}")

# 5. Panel Principal (Usuario Autenticado)
else:
    st.sidebar.title(f"👤 {st.session_state.user['name']}")
    menu = st.sidebar.radio("Navegación", ["Lecciones", "Dashboard & Métricas", "Cerrar Sesión"])
    
    if menu == "Cerrar Sesión":
        st.session_state.user = None
        st.rerun()
        
    elif menu == "Lecciones":
        st.header("📚 Módulos y Mini Lecciones")
        try:
            conn = get_connection()
            lessons = pd.read_sql("SELECT * FROM lessons ORDER BY order_index ASC", conn)
            conn.close()
            
            if lessons.empty:
                st.info("No hay lecciones registradas aún. Puedes insertar datos en Supabase.")
            else:
                selected_title = st.selectbox("Selecciona una lección", lessons["title"])
                lesson = lessons[lessons["title"] == selected_title].iloc[0]
                
                st.subheader(lesson["title"])
                if lesson["video_url"]:
                    st.video(lesson["video_url"])
                st.markdown(lesson["content"] or "")
                
                # Cuestionario
                conn = get_connection()
                q_df = pd.read_sql("SELECT * FROM questions WHERE lesson_id = %s", conn, params=(int(lesson["id"]),))
                conn.close()
                
                if not q_df.empty:
                    st.markdown("---")
                    st.subheader("📝 Cuestionario de Evaluación")
                    answers = {}
                    for _, q in q_df.iterrows():
                        st.write(f"**{q['question_text']}**")
                        opts = [f"A) {q['option_a']}", f"B) {q['option_b']}", f"C) {q['option_c']}"]
                        if q.get("option_d"):
                            opts.append(f"D) {q['option_d']}")
                        answers[q["id"]] = st.radio("Elige una opción:", opts, key=f"q_{q['id']}")
                    
                    if st.button("Enviar Cuestionario"):
                        correct = 0
                        for _, q in q_df.iterrows():
                            if answers[q["id"]][0] == q["correct_option"]:
                                correct += 1
                        score = int((correct / len(q_df)) * 100)
                        passed = score >= 70
                        
                        conn = get_connection()
                        cur = conn.cursor()
                        cur.execute("""
                            INSERT INTO user_progress (user_id, lesson_id, quiz_score, is_completed, completed_at)
                            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                            ON CONFLICT (user_id, lesson_id) 
                            DO UPDATE SET quiz_score = %s, is_completed = %s, completed_at = CURRENT_TIMESTAMP
                        """, (st.session_state.user["id"], int(lesson["id"]), score, passed, score, passed))
                        
                        cur.execute("""
                            INSERT INTO quiz_attempts (user_id, lesson_id, score, passed)
                            VALUES (%s, %s, %s, %s)
                        """, (st.session_state.user["id"], int(lesson["id"]), score, passed))
                        conn.commit()
                        conn.close()
                        
                        if passed:
                            st.success(f"🎉 ¡Felicidades! Aprobaste con {score}%")
                        else:
                            st.error(f"Puntaje: {score}%. Necesitas al menos 70% para aprobar.")
        except Exception as e:
            st.error(f"Error al cargar lecciones: {e}")
            
    elif menu == "Dashboard & Métricas":
        st.header("📊 Dashboard de Progreso")
        try:
            conn = get_connection()
            query = """
                SELECT u.name AS alumno, l.title AS leccion, up.quiz_score AS puntaje,
                       up.is_completed AS aprobado, up.completed_at AS fecha
                FROM user_progress up
                JOIN users u ON up.user_id = u.id
                JOIN lessons l ON up.lesson_id = l.id
            """
            df = pd.read_sql(query, conn)
            conn.close()
            
            if df.empty:
                st.info("Aún no hay progreso registrado.")
            else:
                c1, c2 = st.columns(2)
                c1.metric("Total Lecciones Completadas", int(df["aprobado"].sum()))
                c2.metric("Promedio General", f"{df['puntaje'].mean():.1f} pts")
                st.dataframe(df, use_container_width=True)
                
                # Exportar Excel
                buf_excel = io.BytesIO()
                with pd.ExcelWriter(buf_excel, engine="openpyxl") as writer:
                    df.to_excel(writer, index=False, sheet_name="Métricas")
                
                st.download_button(
                    "📥 Descargar Reporte en Excel",
                    data=buf_excel.getvalue(),
                    file_name="reporte_alumnos.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
        except Exception as e:
            st.error(f"Error al cargar métricas: {e}")

